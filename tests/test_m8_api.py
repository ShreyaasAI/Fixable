"""M8 API: quotas, 429 rate limiting, admin kill/resume (m8-contract §1.6, §3 T7, §13 A2/A3).

Every test runs over a private database (`fresh_db`): global kill and pause would otherwise
cancel other tests' tasks. `m8_client(limits)` builds the app with explicit `AbuseLimits` and a
manual monotonic clock.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from conftest import Clock, build_deps
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry, FakeUser, agents_md
from m4_fakes import make_db_user

from swf import store
from swf.api.app import AppDeps, create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.settings import AbuseLimits
from swf.tokens import TokenCipher

AuthFn = Callable[..., dict[str, str]]

NEW_TASK = {
    "repo": "octo/widgets",
    "base_branch": "main",
    "description": "test_paginate fails on the last page",
    "failing_test": "tests/test_pages.py::test_last_page",
}
REPO = NEW_TASK["repo"]
TODAY = date(2026, 11, 19)  # conftest Clock: 2026-11-19 12:00 UTC
SECONDS_TO_MIDNIGHT = 12 * 3600


class Mono:
    """Manual monotonic clock for the rate limiters (`AppDeps.monotonic`)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Env:
    """What a test needs: users, the app, clients."""

    def __init__(
        self,
        db: Database,
        github: FakeGitHub,
        cipher: TokenCipher,
        auth: AuthFn,
        deps: AppDeps,
        mono: Mono,
    ) -> None:
        self.db, self.github, self.cipher, self.auth = db, github, cipher, auth
        self.deps, self.mono = deps, mono
        self.app = create_app(deps)
        self.client = TestClient(self.app)

    def user(self, *, admin: bool = False) -> FakeUser:
        uid = uuid.uuid4()
        user = FakeUser(id=uid, github_id=random.randint(1, 2**40), login=f"user-{uid.hex[:8]}")
        token = f"gho_{uuid.uuid4().hex}"
        self.github.users[token] = (user.github_id, user.login)
        meta = {"user_name": user.login, "provider_id": str(user.github_id)}
        with self.db.backend() as conn:
            conn.execute(
                "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
                [user.id, f"{user.login}@example.test", json.dumps(meta)],
            )
            conn.execute(
                "insert into profiles (user_id, github_login, is_admin) values (%s, %s, %s)",
                [user.id, user.login, admin],
            )
            conn.execute(
                "insert into github_tokens (user_id, encrypted_token, scopes, github_user_id)"
                " values (%s, %s, %s, %s)",
                [user.id, self.cipher.encrypt(token), ["repo", "read:user"], user.github_id],
            )
        return user

    def post(self, user: FakeUser, client: TestClient | None = None, **body: Any) -> Any:
        return (client or self.client).post(
            "/v1/tasks", json={**NEW_TASK, **body}, headers=self.auth(user)
        )

    def tasks(self, user: FakeUser) -> list[dict[str, Any]]:
        with self.db.backend() as conn:
            return conn.execute(
                "select * from tasks where user_id = %s order by created_seq", [user.id]
            ).fetchall()

    def audit(self, user: FakeUser, action: str | None = None) -> list[dict[str, Any]]:
        with self.db.backend() as conn:
            rows = conn.execute(
                "select action, target, payload from audit_log where user_id = %s order by id",
                [user.id],
            ).fetchall()
        return [r for r in rows if action is None or r["action"] == action]

    def sql(self, query: str, params: list[Any] | None = None) -> None:
        with self.db.backend() as conn:
            conn.execute(query, params)  # type: ignore[arg-type]


M8Client = Callable[..., Env]


@pytest.fixture
def m8_client(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
    auth: AuthFn,
) -> Iterator[M8Client]:
    """`m8_client(limits)` -> Env over `fresh_db` with those limits and a manual monotonic."""
    made: list[Env] = []

    def _make(limits: AbuseLimits | None = None, **replacements: Any) -> Env:
        mono = Mono()
        base = build_deps(fresh_db, verifier, cipher, github, registry, clock)
        # `abuse` and `monotonic` are AppDeps fields from T8 on (red: TypeError until then).
        deps = replace(base, abuse=limits or AbuseLimits(), monotonic=mono, **replacements)
        env = Env(fresh_db, github, cipher, auth, deps, mono)
        made.append(env)
        return env

    yield _make
    for env in made:
        env.client.close()


# --- 1. tasks per day -------------------------------------------------------------------------


def test_tasks_per_day_limit_refuses_the_sixth_create(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(concurrent_tasks=100, create_burst=100))
    user = env.user()
    for _ in range(5):
        assert env.post(user).status_code == 201
    before = (len(env.tasks(user)), len(env.audit(user, "task.create")), len(env.github.requests))
    assert before[:2] == (5, 5)

    resp = env.post(user)

    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "quota_tasks_per_day"
    assert resp.headers["Retry-After"] == str(SECONDS_TO_MIDNIGHT)
    after = (len(env.tasks(user)), len(env.audit(user, "task.create")), len(env.github.requests))
    assert after == before  # no row, no audit row, and no GitHub call


# --- 2. concurrent tasks ----------------------------------------------------------------------


def test_one_concurrent_task_per_user_until_it_is_cancelled(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    first = env.post(user)
    assert first.status_code == 201
    assert first.json()["task"]["status"] == "queued"

    second = env.post(user)
    assert second.status_code == 429
    assert second.json()["detail"] == "quota_concurrent_tasks"
    assert "retry-after" not in second.headers

    task_id = first.json()["task"]["id"]
    cancelled = env.client.post(f"/v1/tasks/{task_id}/cancel", headers=env.auth(user))
    assert cancelled.status_code == 200, cancelled.text
    assert env.post(user).status_code == 201


# --- 3. rejected tasks do not count -----------------------------------------------------------


def test_preflight_rejected_task_does_not_block_the_next_create(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    env.github.repo_host.agents[REPO] = agents_md(memory_mb=8192, cpus=0.5)
    rejected = env.post(user)
    assert rejected.status_code == 201
    assert rejected.json()["task"]["status"] == "rejected"

    env.github.repo_host.agents[REPO] = agents_md(memory_mb=512, cpus=0.5)
    assert env.post(user).status_code == 201


# --- 4. daily cost ----------------------------------------------------------------------------


def test_daily_cost_limit_refuses_create(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    env.sql(
        "insert into quotas_usage (user_id, day, cost_usd) values (%s, %s, 3.0)",
        [user.id, TODAY],
    )
    resp = env.post(user)
    assert resp.status_code == 429
    assert resp.json()["detail"] == "quota_cost_per_day"
    assert resp.headers["Retry-After"] == str(SECONDS_TO_MIDNIGHT)
    assert env.tasks(user) == []


# --- 5. race ----------------------------------------------------------------------------------


def test_parallel_creates_cannot_both_pass_the_concurrent_limit(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(concurrent_tasks=1))
    user = env.user()
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def _go() -> None:
        with TestClient(env.app) as c:
            barrier.wait(timeout=10)
            codes.append(env.post(user, client=c).status_code)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(codes) == [201, 429]
    assert len(env.tasks(user)) == 1


# --- 6. API rate limit ------------------------------------------------------------------------


def test_api_rate_limit_is_per_user_and_refills(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(api_burst=3, api_per_minute=60))
    a, b = env.user(), env.user()
    for _ in range(3):
        assert env.client.get("/v1/tasks", headers=env.auth(a)).status_code == 200
    limited = env.client.get("/v1/tasks", headers=env.auth(a))
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limited"
    assert limited.headers["Retry-After"] == "1"

    assert env.client.get("/v1/tasks", headers=env.auth(b)).status_code == 200
    for _ in range(10):
        assert env.client.get("/healthz").status_code == 200
    bad = env.client.get("/v1/tasks", headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401

    env.mono.t += 1.0
    assert env.client.get("/v1/tasks", headers=env.auth(a)).status_code == 200


# --- 7. create bucket -------------------------------------------------------------------------


def test_create_bucket_limits_posts_before_any_github_call(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(create_burst=1, concurrent_tasks=100))
    user = env.user()
    assert env.post(user).status_code == 201
    seen = len(env.github.requests)

    resp = env.post(user)

    assert resp.status_code == 429
    assert resp.json()["detail"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) >= 1
    assert len(env.github.requests) == seen
    assert len(env.tasks(user)) == 1


# --- 8. admin ---------------------------------------------------------------------------------


def test_non_admin_cannot_kill_or_resume(m8_client: M8Client) -> None:
    env = m8_client()
    user, victim = env.user(), env.user()
    assert env.post(victim).status_code == 201
    for path in ("kill", "resume"):
        resp = env.client.post(f"/v1/admin/{path}", json={}, headers=env.auth(user))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "admin_only"
        resp = env.client.post(f"/v1/admin/{path}", json={})
        assert resp.status_code == 401
    assert [t["status"] for t in env.tasks(victim)] == ["queued"]
    assert env.audit(user) == [] and env.audit(victim, "admin.kill") == []


def test_admin_kills_one_user_then_resumes_them(m8_client: M8Client) -> None:
    env = m8_client()
    admin, a = env.user(admin=True), env.user()
    created = env.post(a)
    assert created.status_code == 201
    task_id = created.json()["task"]["id"]

    resp = env.client.post("/v1/admin/kill", json={"user_id": str(a.id)}, headers=env.auth(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scope": "user", "user_id": str(a.id), "cancelled": [task_id]}
    assert env.tasks(a)[0]["status"] == "cancelled"
    me = env.client.get("/v1/me", headers=env.auth(a))
    assert (me.status_code, me.json()["detail"]) == (403, "account_disabled")
    assert [r["action"] for r in env.audit(admin)] == ["admin.kill"]

    back = env.client.post("/v1/admin/resume", json={"user_id": str(a.id)}, headers=env.auth(admin))
    assert back.status_code == 200, back.text
    assert back.json() == {"scope": "user", "user_id": str(a.id)}
    assert env.client.get("/v1/me", headers=env.auth(a)).status_code == 200


def test_admin_kill_rejects_self_and_unknown_user(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)
    own = env.client.post(
        "/v1/admin/kill", json={"user_id": str(admin.id)}, headers=env.auth(admin)
    )
    assert own.status_code == 422
    assert own.json()["detail"] == "cannot_kill_self"
    unknown = env.client.post(
        "/v1/admin/kill", json={"user_id": str(uuid.uuid4())}, headers=env.auth(admin)
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "not_found"
    unknown = env.client.post(
        "/v1/admin/resume", json={"user_id": str(uuid.uuid4())}, headers=env.auth(admin)
    )
    assert unknown.status_code == 404
    with env.db.backend() as conn:
        assert store.tasks_paused(conn) is False
    assert env.audit(admin) == []


def test_global_kill_pauses_creates_until_resume(m8_client: M8Client) -> None:
    env = m8_client()
    admin, b = env.user(admin=True), env.user()
    queued = env.post(b)
    assert queued.status_code == 201
    task_id = queued.json()["task"]["id"]

    resp = env.client.post("/v1/admin/kill", json={}, headers=env.auth(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scope": "global", "user_id": None, "cancelled": [task_id]}
    paused = env.post(b)
    assert (paused.status_code, paused.json()["detail"]) == (503, "service_paused")
    assert [t["status"] for t in env.tasks(b)] == ["cancelled"]

    back = env.client.post("/v1/admin/resume", json={}, headers=env.auth(admin))
    assert back.status_code == 200, back.text
    assert back.json() == {"scope": "global", "user_id": None}
    assert env.post(b).status_code == 201


# --- review additions (a): create-path re-checks inside the insert transaction ----------------
#
# The re-check cannot be reached by sequential requests: the earlier checks would already have
# refused. So the pre-flight step (which runs between the early checks and the insert
# transaction, with no DB transaction open) is wrapped to apply the state change a concurrent
# admin action would make at that moment. This goes through the public route only, so it does
# not depend on the names of T8's internals; the error shapes follow §1.6 step 5 ("same
# errors") and the existing `account_disabled` 403.


class _HookedPreflight:
    def __init__(self, real: Any, hook: Callable[[], None]) -> None:
        self._real, self._hook, self.calls = real, hook, 0

    def check(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self._hook()
        return self._real.check(*args, **kwargs)


def _hooked(m8_client: M8Client, hook: Callable[[Env], None]) -> tuple[Env, _HookedPreflight]:
    holder: dict[str, Env] = {}
    env = m8_client()
    hooked = _HookedPreflight(env.deps.preflight, lambda: hook(holder["env"]))
    holder["env"] = env
    env.deps.preflight = hooked  # type: ignore[assignment]
    return env, hooked


def test_create_refused_when_paused_after_the_early_checks(m8_client: M8Client) -> None:
    env, hook = _hooked(m8_client, lambda e: e.sql("update system_state set tasks_paused = true"))
    user = env.user()

    resp = env.post(user)

    assert hook.calls == 1  # the early pause check had passed
    assert (resp.status_code, resp.json()["detail"]) == (503, "service_paused")
    assert env.tasks(user) == []
    assert env.audit(user) == []


def test_create_refused_when_disabled_after_the_auth_check(m8_client: M8Client) -> None:
    holder: dict[str, FakeUser] = {}
    env, hook = _hooked(
        m8_client,
        lambda e: e.sql("update profiles set disabled = true where user_id = %s", [holder["u"].id]),
    )
    user = holder["u"] = env.user()

    resp = env.post(user)

    assert hook.calls == 1  # auth had passed
    assert (resp.status_code, resp.json()["detail"]) == (403, "account_disabled")
    assert env.tasks(user) == []
    assert env.audit(user) == []


def test_create_refused_when_quota_is_used_up_after_the_early_checks(
    m8_client: M8Client,
) -> None:
    holder: dict[str, FakeUser] = {}
    env, hook = _hooked(
        m8_client,
        lambda e: e.sql(
            "insert into quotas_usage (user_id, day, tasks) values (%s, %s, 5)",
            [holder["u"].id, TODAY],
        ),
    )
    user = holder["u"] = env.user()

    resp = env.post(user)

    assert hook.calls == 1
    assert (resp.status_code, resp.json()["detail"]) == (429, "quota_tasks_per_day")
    assert resp.headers["Retry-After"] == str(SECONDS_TO_MIDNIGHT)
    assert env.tasks(user) == []
    assert env.audit(user) == []


# --- review additions (b): global kill vs an in-flight create (two connections) ----------------


def test_global_kill_waits_for_an_in_flight_create_and_cancels_its_task(
    fresh_db: Database,
) -> None:
    """The create transaction has run the quota and pause checks (so it holds the `system_state`
    share lock) but not committed. A global kill started now must block until the commit, then
    see and cancel the new task. Written against the store functions the API composes in its
    insert transaction (`check_quota`, `tasks_paused`, `create_task`)."""
    admin, user = make_db_user(fresh_db), make_db_user(fresh_db)
    finished: dict[str, float] = {}
    cancelled: list[uuid.UUID] = []

    def _kill() -> None:
        with fresh_db.backend() as conn:
            cancelled.extend(store.admin_kill(conn, admin.id, None))
        finished["kill"] = time.monotonic()

    killer = threading.Thread(target=_kill)
    with fresh_db.backend() as conn:
        assert store.check_quota(conn, user.id, TODAY, AbuseLimits()) is None
        assert store.tasks_paused(conn) is False
        killer.start()
        killer.join(timeout=1.0)
        assert killer.is_alive(), "kill must wait for the in-flight create"
        task = store.create_task(
            conn,
            user.id,
            repo="octo/widgets",
            base_branch="main",
            description="x",
            failing_test=None,
            day=TODAY,
            status="queued",
            reservation_mb=512,
            reservation_cpu=0.5,
            preflight=None,
        )
        committing = time.monotonic()
    killer.join(timeout=30)
    assert not killer.is_alive()

    assert finished["kill"] >= committing
    assert cancelled == [task.id]
    with fresh_db.backend() as conn:
        row = conn.execute("select status, error from tasks where id = %s", [task.id]).fetchone()
        assert store.tasks_paused(conn) is True
    assert row == {"status": "cancelled", "error": "admin_kill"}
