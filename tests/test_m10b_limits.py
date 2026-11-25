"""M10b §0.5-0.6: the pre-flight token bucket and the cap on open event streams."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
import test_m8_api
from conftest import PERMISSIVE, Clock, build_deps
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry
from m4_fakes import make_db_user, queued_task
from test_m4_sse import open_stream
from test_m8_api import M8Client
from test_m10_preflight_api import preflight

from swf import store
from swf.api.app import create_app
from swf.api.streams import StreamSlots
from swf.auth import JwtVerifier
from swf.db import Database
from swf.settings import AbuseLimits
from swf.tokens import TokenCipher

m8_client = test_m8_api.m8_client

AuthFn = Callable[..., dict[str, str]]


# --- settings ---------------------------------------------------------------------------------


def test_defaults_and_env() -> None:
    d = AbuseLimits()
    assert (d.preflight_per_minute, d.preflight_burst) == (10.0, 5)
    assert (d.sse_streams_per_user, d.sse_streams_total) == (4, 40)
    e = AbuseLimits.from_env(
        {
            "SWF_RATE_PREFLIGHT_PER_MINUTE": "2.5",
            "SWF_RATE_PREFLIGHT_BURST": "3",
            "SWF_SSE_STREAMS_PER_USER": "2",
            "SWF_SSE_STREAMS_TOTAL": "9",
        }
    )
    assert (e.preflight_per_minute, e.preflight_burst) == (2.5, 3)
    assert (e.sse_streams_per_user, e.sse_streams_total) == (2, 9)


# --- pre-flight bucket ------------------------------------------------------------------------


def test_preflight_has_its_own_bucket(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(preflight_per_minute=1.0, preflight_burst=2, concurrent_tasks=9))
    user = env.user()
    assert preflight(env, user).status_code == 200
    assert preflight(env, user).status_code == 200
    third = preflight(env, user)
    assert third.status_code == 429
    assert third.json()["detail"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
    assert env.post(user).status_code == 201  # creating is not limited by the pre-flight bucket


def test_the_preflight_bucket_is_per_user(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(preflight_per_minute=1.0, preflight_burst=1))
    a, b = env.user(), env.user()
    assert preflight(env, a).status_code == 200
    assert preflight(env, a).status_code == 429
    assert preflight(env, b).status_code == 200


def test_the_preflight_bucket_refills(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(preflight_per_minute=1.0, preflight_burst=1))
    user = env.user()
    assert preflight(env, user).status_code == 200
    assert preflight(env, user).status_code == 429
    env.mono.t += 61
    assert preflight(env, user).status_code == 200


# --- stream slots -----------------------------------------------------------------------------


def test_slots_cap_per_user_and_in_total() -> None:
    slots = StreamSlots(per_user=2, total=3)
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a = slots.acquire(u1)
    b = slots.acquire(u1)
    assert a is not None and b is not None
    assert slots.acquire(u1) is None  # per-user cap
    c = slots.acquire(u2)
    assert c is not None
    assert slots.acquire(u3) is None  # total cap
    a.release()
    a.release()  # idempotent: frees one slot only
    assert slots.acquire(u3) is not None
    assert slots.acquire(u3) is None


def test_a_slot_is_freed_when_its_owner_is_collected() -> None:
    slots = StreamSlots(per_user=1, total=1)
    user = uuid.uuid4()

    def never_started() -> Iterator[str]:
        yield "x"

    gen = never_started()
    slot = slots.acquire(user)
    assert slot is not None
    slot.release_with(gen)
    assert slots.acquire(user) is None
    del gen  # a stream object dropped before it ever ran
    assert slots.acquire(user) is not None


# --- the API ----------------------------------------------------------------------------------


@pytest.fixture
def make_sse_client(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
) -> Iterator[Callable[[AbuseLimits], TestClient]]:
    clients: list[TestClient] = []

    def make(limits: AbuseLimits) -> TestClient:
        deps = replace(
            build_deps(fresh_db, verifier, cipher, github, registry, clock, abuse=limits),
            sse_poll_seconds=0.05,
            sse_keepalive_seconds=0.4,
        )
        client = TestClient(create_app(deps))
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.close()


def _limits(per_user: int, total: int) -> AbuseLimits:
    return replace(PERMISSIVE, sse_streams_per_user=per_user, sse_streams_total=total)


def test_a_user_over_the_stream_cap_gets_429_until_a_stream_ends(
    make_sse_client: Callable[[AbuseLimits], TestClient], fresh_db: Database, auth: AuthFn
) -> None:
    client = make_sse_client(_limits(per_user=1, total=10))
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    first = open_stream(client, task.id, auth(user))
    threading.Event().wait(0.5)  # open and idle
    assert first.thread is not None and first.thread.is_alive()

    second = open_stream(client, task.id, auth(user)).wait()
    assert second.status == 429
    assert "too_many_streams" in second.text
    assert second.headers["retry-after"] == "5"

    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
    first.wait()
    assert first.status == 200
    third = open_stream(client, task.id, auth(user)).wait()
    assert third.status == 200  # the first stream's slot was released


def test_the_total_cap_applies_across_users(
    make_sse_client: Callable[[AbuseLimits], TestClient], fresh_db: Database, auth: AuthFn
) -> None:
    client = make_sse_client(_limits(per_user=5, total=1))
    a, b = make_db_user(fresh_db), make_db_user(fresh_db)
    task_a = queued_task(fresh_db, user=a)
    task_b = queued_task(fresh_db, user=b)
    first = open_stream(client, task_a.id, auth(a))
    threading.Event().wait(0.5)
    other = open_stream(client, task_b.id, auth(b)).wait()
    assert other.status == 429
    with fresh_db.backend() as conn:
        store.cancel_task(conn, a.id, task_a.id)
    first.wait()


def test_a_refused_stream_request_holds_no_slot(
    make_sse_client: Callable[[AbuseLimits], TestClient], fresh_db: Database, auth: AuthFn
) -> None:
    client = make_sse_client(_limits(per_user=1, total=1))
    user = make_db_user(fresh_db)
    missing = open_stream(client, uuid.uuid4(), auth(user)).wait()
    assert missing.status == 404
    task = queued_task(fresh_db, user=user)
    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
    assert open_stream(client, task.id, auth(user)).wait().status == 200


def test_a_client_that_leaves_frees_its_slot(
    make_sse_client: Callable[[AbuseLimits], TestClient], fresh_db: Database, auth: AuthFn
) -> None:
    client = make_sse_client(_limits(per_user=1, total=10))
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    with fresh_db.backend() as conn:
        store.append_event(conn, task.id, "message", {"n": 1})
    with client.stream("GET", f"/v1/tasks/{task.id}/events", headers=auth(user)) as resp:
        assert resp.status_code == 200
        next(resp.iter_lines())  # the stream is live; then the client hangs up
    deadline = time.monotonic() + 5
    status = 0
    while time.monotonic() < deadline:
        probe = open_stream(client, task.id, auth(user))
        threading.Event().wait(0.3)
        if probe.thread is not None and not probe.thread.is_alive():
            status = probe.status  # refused (429): the old slot is still held
            threading.Event().wait(0.2)
            continue
        status = 200  # accepted and streaming: the slot was freed
        break
    assert status == 200
    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
