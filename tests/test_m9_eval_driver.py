"""The eval client against the real app, and `records_from_db` over a real event log
(m9-contract §0.16, §1.5, §3 T7). Needs the Postgres container (`fresh_db`); no Docker sandbox,
no network beyond 127.0.0.1.

The real FastAPI app runs under `uvicorn` on `127.0.0.1:0` in a thread (SSE needs a real server).
A helper thread plays the worker with `swf.store`; `EvalClient.drive` plays the user.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import Clock, build_deps
from cryptography.hazmat.primitives.asymmetric import ec
from helpers import FakeGitHub, FakeRegistry, FakeUser, mint
from m4_fakes import make_db_user, queued_task
from swf_eval.client import EVAL_ANSWER, CreateRefused, DriveResult, EvalClient
from swf_eval.metrics import records_from_db

from swf import store
from swf.api.app import create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.settings import AbuseLimits
from swf.tokens import TokenCipher

REPO = "octo/widgets"
NEW_TASK: dict[str, str] = {
    "repo": REPO,
    "base_branch": "main",
    "description": "Page 2 of paginate repeats the last item of page 1.",
    "failing_test": "tests/test_pager.py::test_second_page",
}
LIMITS = AbuseLimits(
    tasks_per_day=100,
    api_per_minute=100_000.0,
    api_burst=100_000,
    create_per_minute=1000.0,
    create_burst=1000,
)  # concurrent_tasks stays at 1: the refusal test relies on it


# --- the real app on a real socket ---------------------------------------------------------


@dataclass
class LiveApi:
    base_url: str
    db: Database
    github: FakeGitHub
    cipher: TokenCipher
    signing_key: ec.EllipticCurvePrivateKey

    def eval_user(self, transport: httpx.BaseTransport | None = None) -> EvalUser:
        user = make_db_user(self.db)
        token = f"gho_{uuid.uuid4().hex}"
        self.github.users[token] = (user.github_id, user.login)
        http = httpx.Client(
            base_url=self.base_url, transport=transport or httpx.HTTPTransport(), timeout=60.0
        )
        client = EvalClient(http, mint(user.claims(), self.signing_key))
        return EvalUser(user, token, client, http)


@dataclass
class EvalUser:
    user: FakeUser
    github_token: str
    client: EvalClient
    http: httpx.Client

    def create(self) -> uuid.UUID:
        return self.client.create_task(**NEW_TASK)


@pytest.fixture
def live_api(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
    signing_key: ec.EllipticCurvePrivateKey,
) -> Iterator[LiveApi]:
    deps = replace(
        build_deps(fresh_db, verifier, cipher, github, registry, clock, abuse=LIMITS),
        sse_poll_seconds=0.05,
        sse_keepalive_seconds=0.2,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(deps), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not (server.started and server.servers):
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield LiveApi(f"http://127.0.0.1:{port}", fresh_db, github, cipher, signing_key)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def eval_user(live_api: LiveApi) -> Iterator[EvalUser]:
    eu = live_api.eval_user()
    try:
        eu.client.link_github_token(eu.github_token)
        yield eu
    finally:
        eu.http.close()


# --- a scripted worker ---------------------------------------------------------------------


class ScriptedWorker(threading.Thread):
    """claim -> plan approval -> command approval -> question -> completed, like `swf.worker`."""

    def __init__(self, db: Database, task_id: uuid.UUID, *, timeout: float = 60.0) -> None:
        super().__init__(daemon=True)
        self.db, self.task_id, self.timeout = db, task_id, timeout
        self.error: BaseException | None = None
        self.answers: list[str | None] = []

    def run(self) -> None:
        try:
            self._play()
        except BaseException as exc:
            self.error = exc

    def _status(self, to: str) -> None:
        with self.db.backend() as conn:
            store.transition_task(conn, self.task_id, to)

    def _wait(self, probe: Callable[[Any], bool], what: str) -> None:
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            with self.db.backend() as conn:
                if probe(conn):
                    return
            time.sleep(0.05)
        raise TimeoutError(f"nobody {what} within {self.timeout} s")

    def _approval(self, kind: str, payload: dict[str, Any]) -> None:
        with self.db.backend() as conn:
            approval = store.create_approval(conn, self.task_id, kind, payload)

        def decided(conn: Any) -> bool:
            row = store.get_approval(conn, approval.id)
            return row is not None and row.status != "pending"

        self._wait(decided, f"decided the {kind} approval")

    def _play(self) -> None:
        with self.db.backend() as conn:
            claimed = store.claim_next_task(conn, worker_id="m9-test-worker", lease_seconds=300)
        assert claimed is not None and claimed.id == self.task_id
        self._status("investigating")
        self._status("awaiting_plan_approval")
        self._approval("plan", {"plan": "slice items[(page - 1) * size : page * size]"})
        self._status("executing")
        self._status("awaiting_command_approval")
        self._approval("command", {"command": "python -m pytest -q", "reasons": []})
        self._status("executing")
        self._status("awaiting_input")
        with self.db.backend() as conn:
            question = store.create_question(conn, self.task_id, "Which page size should I use?")

        def answered(conn: Any) -> bool:
            row = store.get_question(conn, question.id)
            if row is not None and row.answer is not None:
                self.answers.append(row.answer)
                return True
            return False

        self._wait(answered, "answered the question")
        self._status("executing")
        self._status("verifying")
        self._status("confirming")
        self._status("completed")


def run_worker(live: LiveApi, task_id: uuid.UUID) -> ScriptedWorker:
    worker = ScriptedWorker(live.db, task_id)
    worker.start()
    return worker


def task_status(db: Database, task_id: uuid.UUID) -> str | None:
    with db.backend() as conn:
        return store.get_task_status(conn, task_id)


# --- EvalClient.drive ----------------------------------------------------------------------


def test_drive_approves_both_approvals_and_answers_the_question(
    live_api: LiveApi, eval_user: EvalUser
) -> None:
    task_id = eval_user.create()
    worker = run_worker(live_api, task_id)
    result = eval_user.client.drive(task_id, deadline=time.monotonic() + 60)
    worker.join(timeout=30)
    assert worker.error is None, repr(worker.error)
    assert result == DriveResult(
        status="completed", approvals={"plan": 1, "command": 1}, questions=1
    )
    assert worker.answers == [EVAL_ANSWER]
    assert EVAL_ANSWER == "No human is available. Use your best judgement and continue."
    with live_api.db.backend() as conn:
        rows = conn.execute(
            "select kind, status, decided_by from approvals where task_id = %s order by created_at",
            [task_id],
        ).fetchall()
        audit = conn.execute(
            "select action, user_id from audit_log where action in"
            " ('approval.decide', 'question.answer') and user_id = %s order by id",
            [eval_user.user.id],
        ).fetchall()
        question = conn.execute(
            "select answer from questions where task_id = %s", [task_id]
        ).fetchone()
    assert [(r["kind"], r["status"]) for r in rows] == [
        ("plan", "approved"),
        ("command", "approved"),
    ]
    assert all(r["decided_by"] == eval_user.user.id for r in rows)  # decided by the submitter
    assert sorted(a["action"] for a in audit) == ["approval.decide"] * 2 + ["question.answer"]
    assert question is not None and question["answer"] == EVAL_ANSWER


class DropAfterFirstApproval(httpx.BaseTransport):
    """Delivers whole SSE events, then cuts the first events connection right after the
    `approval_requested` event: cleanly (`eof`) or with a read error (`error`)."""

    def __init__(self, mode: str) -> None:
        self.inner = httpx.HTTPTransport()
        self.mode = mode
        self.requests: list[httpx.Request] = []
        self.cut_after_id: int | None = None
        self._armed = True

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.inner.handle_request(request)
        if self._armed and request.method == "GET" and request.url.path.endswith("/events"):
            self._armed = False
            assert isinstance(response.stream, httpx.SyncByteStream)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_CutStream(response.stream, self),
                extensions=response.extensions,
            )
        return response

    def close(self) -> None:
        self.inner.close()

    def paths(self, method: str, fragment: str) -> list[str]:
        return [r.url.path for r in self.requests if r.method == method and fragment in r.url.path]


class _CutStream(httpx.SyncByteStream):
    def __init__(self, inner: httpx.SyncByteStream, owner: DropAfterFirstApproval) -> None:
        self.inner, self.owner = inner, owner

    def __iter__(self) -> Iterator[bytes]:
        buf = b""
        for chunk in self.inner:
            buf += chunk
            while b"\n\n" in buf:
                event, buf = buf.split(b"\n\n", 1)
                yield event + b"\n\n"
                if b"event: approval_requested" in event:
                    match = re.search(rb"id: (\d+)", event)
                    assert match is not None
                    self.owner.cut_after_id = int(match.group(1))
                    self.inner.close()
                    if self.owner.mode == "error":
                        raise httpx.ReadError("connection dropped by the test")
                    return
        if buf:
            yield buf

    def close(self) -> None:
        self.inner.close()


@pytest.mark.parametrize("mode", ["eof", "error"])
def test_a_dropped_stream_resumes_with_last_event_id_and_handles_no_event_twice(
    live_api: LiveApi, mode: str
) -> None:
    transport = DropAfterFirstApproval(mode)
    eu = live_api.eval_user(transport)
    try:
        eu.client.link_github_token(eu.github_token)
        task_id = eu.create()
        worker = run_worker(live_api, task_id)
        result = eu.client.drive(task_id, deadline=time.monotonic() + 60)
        worker.join(timeout=30)
    finally:
        eu.http.close()
    assert worker.error is None, repr(worker.error)
    assert result == DriveResult(
        status="completed", approvals={"plan": 1, "command": 1}, questions=1
    )
    assert transport.cut_after_id is not None
    events = [r for r in transport.requests if r.url.path.endswith(f"/{task_id}/events")]
    assert len(events) >= 2
    assert "last-event-id" not in {k.lower() for k in events[0].headers}
    assert events[1].headers["Last-Event-ID"] == str(transport.cut_after_id)
    approvals = transport.paths("POST", "/approvals/")
    assert len(approvals) == 2 and len(set(approvals)) == 2  # each approval decided once
    assert len(transport.paths("POST", "/answers/")) == 1


def test_a_passed_deadline_cancels_the_task(live_api: LiveApi, eval_user: EvalUser) -> None:
    task_id = eval_user.create()
    assert task_status(live_api.db, task_id) == "queued"
    result = eval_user.client.drive(task_id, deadline=100.0, clock=lambda: 200.0)
    assert task_status(live_api.db, task_id) == "cancelled"
    assert result.status == "cancelled"
    assert result.approvals == {} and result.questions == 0


def test_cancel_cancels_a_queued_task(live_api: LiveApi, eval_user: EvalUser) -> None:
    task_id = eval_user.create()
    eval_user.client.cancel(task_id)
    assert task_status(live_api.db, task_id) == "cancelled"


def test_create_refused_carries_the_status_and_the_detail(
    live_api: LiveApi, eval_user: EvalUser
) -> None:
    first = eval_user.create()
    assert task_status(live_api.db, first) == "queued"
    with pytest.raises(CreateRefused) as err:
        eval_user.create()  # one concurrent task per user (m8 default)
    assert err.value.status == 429
    assert err.value.detail == "quota_concurrent_tasks"
    eval_user.client.cancel(first)
    assert task_status(live_api.db, eval_user.create()) == "queued"  # free again once terminal


def test_create_task_sends_the_task_fields_and_returns_the_id(
    live_api: LiveApi, eval_user: EvalUser
) -> None:
    task_id = eval_user.create()
    with live_api.db.backend() as conn:
        row = conn.execute("select * from tasks where id = %s", [task_id]).fetchone()
    assert row is not None and row["user_id"] == eval_user.user.id
    assert (row["repo"], row["base_branch"]) == (REPO, "main")
    assert row["description"] == NEW_TASK["description"]
    assert row["failing_test"] == NEW_TASK["failing_test"]


def test_link_github_token_stores_the_token_encrypted(live_api: LiveApi) -> None:
    eu = live_api.eval_user()
    try:
        with live_api.db.backend() as conn:
            assert store.get_encrypted_github_token(conn, eu.user.id) is None
        eu.client.link_github_token(eu.github_token)
        with live_api.db.backend() as conn:
            blob = store.get_encrypted_github_token(conn, eu.user.id)
    finally:
        eu.http.close()
    assert blob is not None and eu.github_token not in blob
    assert live_api.cipher.decrypt(blob) == eu.github_token


# --- records_from_db -----------------------------------------------------------------------

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)


def set_status_time(db: Database, task_id: uuid.UUID, to: str, when: datetime) -> None:
    with db.backend() as conn:
        conn.execute(
            "update events set created_at = %s where task_id = %s and type = 'status'"
            " and payload->>'to' = %s",
            [when, task_id, to],
        )


def test_records_from_db_reads_the_metrics_out_of_the_event_log(fresh_db: Database) -> None:
    owner = make_db_user(fresh_db)
    done = queued_task(fresh_db, user=owner)
    failed = queued_task(fresh_db, user=owner)
    cancelled = queued_task(fresh_db, user=owner)
    with fresh_db.backend() as conn:
        first = store.claim_next_task(conn, worker_id="w", lease_seconds=60)
        second = store.claim_next_task(conn, worker_id="w", lease_seconds=60)
        assert first is not None and first.id == done.id
        assert second is not None and second.id == failed.id
        store.cancel_task(conn, owner.id, cancelled.id)
    with fresh_db.backend() as conn:
        store.transition_task(conn, done.id, "investigating")
        for role in ("system", "user", "assistant", "assistant", "assistant"):
            store.append_event(conn, done.id, "message", {"role": role, "content": "x"})
        for chunks, ms in ((3, 1500), (2, 500)):
            store.append_event(
                conn,
                done.id,
                "guard_check",
                {"source": "tool_output", "flagged": False, "categories": [],
                 "latency_ms": ms, "chunks": chunks, "cached": False},
            )  # fmt: skip
        for attempt in (1, 2):
            store.append_event(
                conn, done.id, "retry", {"step": "model", "attempt": attempt, "error": "429"}
            )
        for kind, payload in (
            ("plan", {"plan": "p"}),
            ("command", {"command": "a"}),
            ("command", {"command": "b"}),
            ("guard_flag", {"reason": "guardrail"}),
        ):
            approval = store.create_approval(conn, done.id, kind, payload)
            # one pending approval per task: settle each before the next is requested
            store.expire_approval(conn, approval.id)
        store.create_question(conn, done.id, "which one?")
        store.add_task_cost(conn, done.id, 0.25)
        for to in ("awaiting_plan_approval", "executing", "verifying", "confirming", "opening_pr"):
            store.transition_task(conn, done.id, to)
        store.record_pr(
            conn,
            done.id,
            url="https://github.com/o/r/pull/7",
            number=7,
            branch=f"agent/{done.id}",
            commit_sha="a" * 40,
        )
        store.transition_task(conn, done.id, "completed")
        store.transition_task(conn, failed.id, "failed", error="clone_failed: boom")
    set_status_time(fresh_db, done.id, "preparing", T0)
    set_status_time(fresh_db, done.id, "completed", T0 + timedelta(seconds=95, milliseconds=500))
    set_status_time(fresh_db, failed.id, "preparing", T0 + timedelta(seconds=10))
    set_status_time(fresh_db, failed.id, "failed", T0 + timedelta(seconds=12))

    ids = {
        str(done.id): ("single", "01-paginate-off-by-one", "eval-a"),
        str(failed.id): ("concurrent", "02-mutable-default", "eval-b"),
        str(cancelled.id): ("single", "03-none-last-name", "eval-a"),
    }
    with fresh_db.backend() as conn:
        records = {r.task_id: r for r in records_from_db(conn, ids)}
    assert set(records) == set(ids)

    d = records[str(done.id)]
    assert (d.pass_, d.seed, d.user) == ("single", "01-paginate-off-by-one", "eval-a")
    assert d.status == "completed" and d.error is None
    assert d.agent_calls == 3  # only role "assistant" messages
    assert d.guard_chunks == 5 and d.guard_seconds == pytest.approx(2.0)
    assert d.retries == 2 and d.questions == 1
    assert {k: v for k, v in d.approvals.items() if v} == {"plan": 1, "command": 2, "guard_flag": 1}
    assert d.cost_usd == pytest.approx(0.25)
    assert d.pr_url == "https://github.com/o/r/pull/7"
    assert d.claimed_at is not None and d.finished_at is not None
    assert d.finished_at - d.claimed_at == pytest.approx(95.5)
    assert d.wall_seconds == pytest.approx(95.5)
    assert d.resolution is None and d.peak_mem_mb is None and not d.resolved

    f = records[str(failed.id)]
    assert (f.pass_, f.seed, f.user) == ("concurrent", "02-mutable-default", "eval-b")
    assert f.status == "failed" and f.error == "clone_failed: boom"
    assert f.pr_url is None and f.agent_calls == 0 and f.guard_chunks == 0 and f.retries == 0
    assert f.wall_seconds == pytest.approx(2.0)

    c = records[str(cancelled.id)]
    assert c.status == "cancelled"
    assert c.claimed_at is None and c.wall_seconds is None  # never claimed: queue time is no wall
