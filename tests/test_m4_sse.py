"""`GET /v1/tasks/{id}/events` server-sent events (m4-contract §9)."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from conftest import Clock, build_deps
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry, FakeUser
from m4_fakes import make_db_user, queued_task

from swf import store
from swf.api.app import create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.tokens import TokenCipher

pytest_plugins = ["m4_fakes"]

AuthFn = Callable[..., dict[str, str]]


@pytest.fixture
def sse_client(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
) -> Iterator[TestClient]:
    deps = build_deps(fresh_db, verifier, cipher, github, registry, clock)
    deps.sse_poll_seconds = 0.05
    deps.sse_keepalive_seconds = 0.4
    with TestClient(create_app(deps)) as client:
        yield client


@dataclass
class Stream:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    thread: threading.Thread | None = None

    def wait(self, timeout: float = 20.0) -> Stream:
        assert self.thread is not None
        self.thread.join(timeout)
        if self.thread.is_alive():
            pytest.fail("the event stream did not end")
        return self

    @property
    def events(self) -> list[dict[str, Any]]:
        return parse(self.text)


def open_stream(
    client: TestClient,
    task_id: Any,
    headers: dict[str, str],
    *,
    params: dict[str, Any] | None = None,
) -> Stream:
    stream = Stream()

    def target() -> None:
        with client.stream(
            "GET", f"/v1/tasks/{task_id}/events", headers=headers, params=params
        ) as response:
            stream.status = response.status_code
            stream.headers = {k.lower(): v for k, v in response.headers.items()}
            stream.text = "".join(response.iter_text())

    stream.thread = threading.Thread(target=target, daemon=True)
    stream.thread.start()
    return stream


def parse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        events.append(
            {"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])}
        )
    return events


def seeded(db: Database, notes: int = 2) -> tuple[FakeUser, store.Task]:
    """A cancelled (terminal) task with a status event, `notes` notes and a final status."""
    user = make_db_user(db)
    task = queued_task(db, user=user)
    with db.backend() as conn:
        for n in range(notes):
            store.append_event(conn, task.id, "message", {"n": n})
        store.cancel_task(conn, user.id, task.id)
    return user, task


def test_the_stream_needs_a_token(sse_client: TestClient, fresh_db: Database) -> None:
    _, task = seeded(fresh_db)
    assert sse_client.get(f"/v1/tasks/{task.id}/events").status_code == 401


def test_another_users_task_is_a_404_before_streaming(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    _, task = seeded(fresh_db)
    stranger = make_db_user(fresh_db)
    other = open_stream(sse_client, task.id, auth(stranger)).wait()
    assert other.status == 404
    assert "text/event-stream" not in other.headers.get("content-type", "")
    missing = open_stream(sse_client, uuid.uuid4(), auth(stranger)).wait()
    assert missing.status == 404


def test_events_are_framed_with_id_event_and_json_data(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = seeded(fresh_db)
    stream = open_stream(sse_client, task.id, auth(user)).wait()
    assert stream.status == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-store"
    assert stream.headers["x-accel-buffering"] == "no"
    assert stream.text.startswith("id: 0\nevent: status\ndata: ")
    assert stream.text.endswith("\n\n")
    events = stream.events
    assert [e["id"] for e in events] == [0, 1, 2, 3]
    assert [e["event"] for e in events] == ["status", "message", "message", "status"]
    first, note, _, last = (e["data"] for e in events)
    assert (first["from"], first["to"]) == (None, "queued")
    assert note["n"] == 0
    assert last["to"] == "cancelled"
    for e in events:
        assert isinstance(e["data"]["created_at"], str)


def test_resume_from_the_last_event_id_header(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = seeded(fresh_db)
    stream = open_stream(sse_client, task.id, {**auth(user), "Last-Event-ID": "1"}).wait()
    assert [e["id"] for e in stream.events] == [2, 3]


def test_resume_from_the_after_query(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = seeded(fresh_db)
    stream = open_stream(sse_client, task.id, auth(user), params={"after": 1}).wait()
    assert [e["id"] for e in stream.events] == [2, 3]


def test_the_header_wins_over_the_query(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = seeded(fresh_db)
    stream = open_stream(
        sse_client, task.id, {**auth(user), "Last-Event-ID": "2"}, params={"after": 0}
    ).wait()
    assert [e["id"] for e in stream.events] == [3]


def test_resuming_after_the_last_event_of_a_finished_task_ends_at_once(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = seeded(fresh_db)
    stream = open_stream(sse_client, task.id, {**auth(user), "Last-Event-ID": "3"}).wait()
    assert stream.status == 200
    assert stream.events == []


def test_the_stream_stays_open_until_the_task_finishes_and_delivers_late_events(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    stream = open_stream(sse_client, task.id, auth(user))
    threading.Event().wait(0.5)  # the stream is open and idle by now
    assert stream.thread is not None
    assert stream.thread.is_alive()
    with fresh_db.backend() as conn:
        store.append_event(conn, task.id, "message", {"n": "late"})
    threading.Event().wait(0.3)
    assert stream.thread.is_alive()  # a non-terminal task never ends the stream
    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
    stream.wait()
    events = stream.events
    assert [e["id"] for e in events] == [0, 1, 2]
    assert events[1]["data"]["n"] == "late"
    assert events[2]["data"]["to"] == "cancelled"


def test_every_event_up_to_the_terminal_status_is_sent_before_the_stream_ends(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    stream = open_stream(sse_client, task.id, auth(user))
    with fresh_db.backend() as conn:
        for n in range(40):
            store.append_event(conn, task.id, "message", {"n": n})
        store.cancel_task(conn, user.id, task.id)
    stream.wait()
    assert [e["id"] for e in stream.events] == list(range(42))
    assert stream.events[-1]["data"]["to"] == "cancelled"


def test_an_idle_stream_sends_keepalive_comments(
    sse_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    stream = open_stream(sse_client, task.id, {**auth(user), "Last-Event-ID": "0"})
    threading.Event().wait(1.5)  # more than three keepalive periods
    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
    stream.wait()
    assert stream.text.count(": ping\n\n") >= 2
    assert [e["id"] for e in stream.events] == [1]
