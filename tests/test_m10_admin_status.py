"""M10a T4: `GET /v1/admin/status` (m10a-contract §1.4).

Admin-only read model: pause state with who and when, queued and active counts, and the users
list (disabled first, then by latest task, at most 100). It is read-only: no audit row and no
wait on a lock held on `system_state`.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import test_m8_api
from test_m8_api import Env, M8Client

PATH = "/v1/admin/status"

m8_client = test_m8_api.m8_client  # reuse the M8 app fixture


def status(env: Env, admin: Any) -> Any:
    return env.client.get(PATH, headers=env.auth(admin))


def _task(env: Env, user: Any, st: str, *, created: str | None = None) -> uuid.UUID:
    with env.db.backend() as conn:
        row = conn.execute(
            "insert into tasks (user_id, repo, base_branch, description, status, created_at)"
            " values (%s, 'octo/widgets', 'main', 'd', %s, coalesce(%s::timestamptz, now()))"
            " returning id",
            [user.id, st, created],
        ).fetchone()
    assert row is not None
    return row["id"]  # type: ignore[no-any-return]


def test_non_admin_gets_403_admin_only_and_anonymous_401(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    resp = status(env, user)
    assert (resp.status_code, resp.json()["detail"]) == (403, "admin_only")
    assert env.client.get(PATH).status_code == 401


def test_fresh_system_is_not_paused(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)

    body = status(env, admin).json()

    assert body["tasks_paused"] is False
    assert body["queued"] == 0
    assert body["active"] == 0
    assert str(admin.id) in {u["user_id"] for u in body["users"]}


def test_status_reports_who_paused_and_when_after_a_global_kill(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)
    killed = env.client.post("/v1/admin/kill", json={}, headers=env.auth(admin))
    assert killed.status_code == 200, killed.text

    body = status(env, admin).json()

    assert body["tasks_paused"] is True
    assert body["updated_by"] == str(admin.id)
    assert body["updated_at"] is not None

    env.client.post("/v1/admin/resume", json={}, headers=env.auth(admin))
    assert status(env, admin).json()["tasks_paused"] is False


def test_status_counts_queued_and_active_tasks(m8_client: M8Client) -> None:
    env = m8_client()
    admin, a, b = env.user(admin=True), env.user(), env.user()
    for st in ("queued", "queued", "executing", "awaiting_input", "completed", "cancelled"):
        _task(env, a, st)
    _task(env, b, "preparing")
    _task(env, b, "rejected")

    body = status(env, admin).json()

    assert body["queued"] == 2
    assert body["active"] == 3
    by_id = {u["user_id"]: u for u in body["users"]}
    assert by_id[str(a.id)]["open_tasks"] == 4
    assert by_id[str(b.id)]["open_tasks"] == 1
    assert by_id[str(admin.id)]["open_tasks"] == 0


def test_users_carry_login_admin_and_disabled_flags(m8_client: M8Client) -> None:
    env = m8_client()
    admin, a = env.user(admin=True), env.user()
    env.sql("update profiles set disabled = true where user_id = %s", [a.id])

    by_id = {u["user_id"]: u for u in status(env, admin).json()["users"]}

    assert by_id[str(a.id)]["github_login"] == a.login
    assert by_id[str(a.id)]["disabled"] is True
    assert by_id[str(a.id)]["is_admin"] is False
    assert by_id[str(admin.id)]["is_admin"] is True
    assert by_id[str(admin.id)]["disabled"] is False


def test_users_are_ordered_disabled_first_then_by_latest_task(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)
    old, mid, new, off = env.user(), env.user(), env.user(), env.user()
    _task(env, old, "completed", created="2026-11-01T00:00:00Z")
    _task(env, mid, "completed", created="2026-11-10T00:00:00Z")
    _task(env, new, "completed", created="2026-11-20T00:00:00Z")
    _task(env, off, "completed", created="2026-10-01T00:00:00Z")
    env.sql("update profiles set disabled = true where user_id = %s", [off.id])

    order = [u["user_id"] for u in status(env, admin).json()["users"]]

    assert order[0] == str(off.id)  # disabled first, whatever its latest task
    assert order.index(str(new.id)) < order.index(str(mid.id)) < order.index(str(old.id))


def test_users_list_is_capped_at_100(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)
    for _ in range(104):
        env.user()
    assert len(status(env, admin).json()["users"]) == 100


def test_status_writes_no_audit_row(m8_client: M8Client) -> None:
    env = m8_client()
    admin = env.user(admin=True)
    with env.db.backend() as conn:
        before = conn.execute("select count(*) as n from audit_log").fetchone()

    assert status(env, admin).status_code == 200

    with env.db.backend() as conn:
        assert conn.execute("select count(*) as n from audit_log").fetchone() == before
    assert env.audit(admin) == []


def test_status_does_not_wait_for_a_lock_on_system_state(m8_client: M8Client) -> None:
    """A transaction holding `select ... from system_state for update` (as a kill does) must not
    block the read: the status call finishes inside a short deadline."""
    env = m8_client()
    admin = env.user(admin=True)
    assert status(env, admin).status_code == 200  # warm-up: profile row exists, app is hot
    result: list[int] = []

    def _read() -> None:
        result.append(status(env, admin).status_code)

    with env.db.backend() as conn:
        conn.execute("select * from public.system_state for update")
        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout=5)
        finished = not thread.is_alive()
    thread.join(timeout=30)  # let a (wrongly) blocked reader finish once the lock is released

    assert finished, "GET /v1/admin/status blocked on the system_state row lock"
    assert result == [200]
