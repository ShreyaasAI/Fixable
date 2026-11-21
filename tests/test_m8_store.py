"""M8 T5: migration `system_state` and the quota / kill-switch store functions
(m8-contract §1.4, §1.5). Private database clones only (`fresh_db`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from m4_fakes import make_db_user, queued_task, task_row

from swf import store
from swf.db import Database
from swf.settings import AbuseLimits


def set_usage(
    db: Database, user_id: uuid.UUID, *, tasks: int = 0, cost: float = 0.0, days_ago: int = 0
) -> None:
    day = datetime.now(UTC).date() - timedelta(days=days_ago)
    with db.backend() as conn:
        conn.execute(
            "insert into public.quotas_usage (user_id, day, tasks, cost_usd)"
            " values (%s, %s, %s, %s) on conflict (user_id, day)"
            " do update set tasks = excluded.tasks, cost_usd = excluded.cost_usd",
            [user_id, day, tasks, cost],
        )


def quota(db: Database, user_id: uuid.UUID, limits: AbuseLimits | None = None) -> str | None:
    with db.backend() as conn:
        return store.check_quota(conn, user_id, datetime.now(UTC).date(), limits or AbuseLimits())


def audit_rows(db: Database, action: str) -> list[dict[str, object]]:
    with db.backend() as conn:
        rows = conn.execute(
            "select user_id, action, target, payload from public.audit_log where action = %s",
            [action],
        ).fetchall()
    return [dict(r) for r in rows]


def paused(db: Database) -> bool:
    with db.backend() as conn:
        return store.tasks_paused(conn)


# --- 1. migration ---------------------------------------------------------------------------


def test_system_state_has_one_unpaused_row(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        rows = conn.execute("select id, tasks_paused from public.system_state").fetchall()
    assert [(r["id"], r["tasks_paused"]) for r in rows] == [(True, False)]


def test_system_state_refuses_a_second_row(fresh_db: Database) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_db.backend() as conn:
        conn.execute("insert into public.system_state default values")


def test_system_state_is_backend_only(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        rls = conn.execute(
            "select relrowsecurity from pg_class where oid = 'public.system_state'::regclass"
        ).fetchone()
        assert rls is not None and rls["relrowsecurity"] is True
        for role in ("authenticated", "anon"):
            row = conn.execute(
                "select has_table_privilege(%s, 'public.system_state', 'select') as ok", [role]
            ).fetchone()
            assert row is not None and row["ok"] is False


# --- 2. check_quota -------------------------------------------------------------------------


def test_fresh_user_is_within_quota(fresh_db: Database) -> None:
    assert quota(fresh_db, make_db_user(fresh_db).id) is None


def test_tasks_per_day_reached(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    set_usage(fresh_db, user.id, tasks=5)
    assert quota(fresh_db, user.id) == "quota_tasks_per_day"


def test_raised_tasks_per_day_limit_allows_the_same_usage(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    set_usage(fresh_db, user.id, tasks=5)
    assert quota(fresh_db, user.id, AbuseLimits(tasks_per_day=6)) is None


def test_cost_per_day_reached(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    set_usage(fresh_db, user.id, cost=3.0)
    assert quota(fresh_db, user.id) == "quota_cost_per_day"


def test_a_queued_task_hits_the_concurrency_limit(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    queued_task(fresh_db, user=user)
    assert quota(fresh_db, user.id) == "quota_concurrent_tasks"


def test_rejected_and_completed_tasks_do_not_count_as_concurrent(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    queued_task(fresh_db, user=user, status="rejected")
    queued_task(fresh_db, user=user, status="completed")
    assert quota(fresh_db, user.id) is None


def test_usage_from_yesterday_is_ignored(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    set_usage(fresh_db, user.id, tasks=5, cost=3.0, days_ago=1)
    assert quota(fresh_db, user.id) is None


def test_tasks_per_day_takes_precedence_over_concurrent(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    set_usage(fresh_db, user.id, tasks=5)
    queued_task(fresh_db, user=user)
    assert quota(fresh_db, user.id) == "quota_tasks_per_day"


# --- 3-6. admin_kill / admin_resume ---------------------------------------------------------


def test_kill_user_cancels_their_open_tasks_and_disables_them(fresh_db: Database) -> None:
    admin, a, b = (make_db_user(fresh_db) for _ in range(3))
    queued = queued_task(fresh_db, user=a)
    executing = queued_task(fresh_db, user=a, status="executing")
    waiting = queued_task(fresh_db, user=a, status="awaiting_input")
    done = queued_task(fresh_db, user=a, status="completed")
    other = queued_task(fresh_db, user=b, status="executing")
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, executing.id, "command", {"command": "ls"})
        question = store.create_question(conn, waiting.id, "which file?")

    with fresh_db.backend() as conn:
        cancelled = store.admin_kill(conn, admin.id, a.id)

    assert cancelled == [queued.id, executing.id, waiting.id]
    for t in (queued, executing, waiting):
        row = task_row(fresh_db, t.id)
        assert (row["status"], row["error"]) == ("cancelled", "admin_kill")
    assert task_row(fresh_db, done.id)["status"] == "completed"
    assert task_row(fresh_db, other.id)["status"] == "executing"
    with fresh_db.backend() as conn:
        expired_approval = store.get_approval(conn, approval.id)
        expired_question = store.get_question(conn, question.id)
        profile_a = store.get_profile(conn, a.id)
        profile_b = store.get_profile(conn, b.id)
    assert expired_approval is not None and expired_approval.status == "expired"
    assert expired_question is not None and expired_question.expired_at is not None
    assert profile_a is not None and profile_a.disabled is True
    assert profile_b is not None and profile_b.disabled is False
    (row,) = audit_rows(fresh_db, "admin.kill")
    assert row["user_id"] == admin.id
    assert row["target"] == str(a.id)
    assert row["payload"] == {"scope": "user", "cancelled": 3}


def test_kill_unknown_user_raises_not_found(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    with pytest.raises(store.NotFound), fresh_db.backend() as conn:
        store.admin_kill(conn, admin.id, uuid.uuid4())


def test_global_kill_pauses_claims_and_resume_restores_them(fresh_db: Database) -> None:
    admin, a, b = (make_db_user(fresh_db) for _ in range(3))
    ta = queued_task(fresh_db, user=a)
    tb = queued_task(fresh_db, user=b, status="executing")

    with fresh_db.backend() as conn:
        cancelled = store.admin_kill(conn, admin.id, None)
    assert set(cancelled) == {ta.id, tb.id}
    for t in (ta, tb):
        row = task_row(fresh_db, t.id)
        assert (row["status"], row["error"]) == ("cancelled", "admin_kill")
    assert paused(fresh_db) is True
    with fresh_db.backend() as conn:
        state = conn.execute("select updated_by from public.system_state").fetchone()
    assert state is not None and state["updated_by"] == admin.id
    (row,) = audit_rows(fresh_db, "admin.kill")
    assert row["target"] == "global"
    assert row["payload"] == {"scope": "global", "cancelled": 2}

    fresh = queued_task(fresh_db, user=a)
    with fresh_db.backend() as conn:
        assert store.claim_next_task(conn, worker_id="w1", lease_seconds=60) is None

    with fresh_db.backend() as conn:
        store.admin_resume(conn, admin.id, None)
    assert paused(fresh_db) is False
    with fresh_db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=60)
    assert claimed is not None and claimed.id == fresh.id
    (row,) = audit_rows(fresh_db, "admin.resume")
    assert row["user_id"] == admin.id
    assert row["target"] == "global"


def test_resume_user_clears_disabled(fresh_db: Database) -> None:
    admin, a = make_db_user(fresh_db), make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        store.admin_kill(conn, admin.id, a.id)
        store.admin_resume(conn, admin.id, a.id)
        profile = store.get_profile(conn, a.id)
    assert profile is not None and profile.disabled is False
    (row,) = audit_rows(fresh_db, "admin.resume")
    assert row["target"] == str(a.id)


def test_resume_unknown_user_raises_not_found(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    with pytest.raises(store.NotFound), fresh_db.backend() as conn:
        store.admin_resume(conn, admin.id, uuid.uuid4())


def test_global_kill_audit_actor_is_the_admin(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        store.admin_kill(conn, admin.id, None)
    (row,) = audit_rows(fresh_db, "admin.kill")
    assert row["user_id"] == admin.id


def test_global_resume_audit_actor_and_payload(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        store.admin_resume(conn, admin.id, None)
    (row,) = audit_rows(fresh_db, "admin.resume")
    assert row["user_id"] == admin.id
    assert row["payload"] == {"scope": "global"}


def test_user_resume_audit_actor_and_payload(fresh_db: Database) -> None:
    admin, a = make_db_user(fresh_db), make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        store.admin_resume(conn, admin.id, a.id)
    (row,) = audit_rows(fresh_db, "admin.resume")
    assert row["user_id"] == admin.id
    assert row["payload"] == {"scope": "user"}


def delete_system_state(db: Database) -> None:
    with db.backend() as conn:
        conn.execute("delete from public.system_state")


def test_tasks_paused_raises_when_the_row_is_missing(fresh_db: Database) -> None:
    delete_system_state(fresh_db)
    with pytest.raises(RuntimeError), fresh_db.backend() as conn:
        store.tasks_paused(conn)


def test_global_kill_raises_when_the_row_is_missing(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    delete_system_state(fresh_db)
    with pytest.raises(RuntimeError), fresh_db.backend() as conn:
        store.admin_kill(conn, admin.id, None)
    assert audit_rows(fresh_db, "admin.kill") == []


def test_global_resume_raises_when_the_row_is_missing(fresh_db: Database) -> None:
    admin = make_db_user(fresh_db)
    delete_system_state(fresh_db)
    with pytest.raises(RuntimeError), fresh_db.backend() as conn:
        store.admin_resume(conn, admin.id, None)
    assert audit_rows(fresh_db, "admin.resume") == []


def test_claim_skips_a_queued_task_of_a_disabled_user(fresh_db: Database) -> None:
    a = make_db_user(fresh_db)
    queued_task(fresh_db, user=a)
    with fresh_db.backend() as conn:
        conn.execute("update public.profiles set disabled = true where user_id = %s", [a.id])
    with fresh_db.backend() as conn:
        assert store.claim_next_task(conn, worker_id="w1", lease_seconds=60) is None


def test_claim_still_serves_other_users_when_one_is_disabled(fresh_db: Database) -> None:
    a, b = make_db_user(fresh_db), make_db_user(fresh_db)
    queued_task(fresh_db, user=a)
    tb = queued_task(fresh_db, user=b)
    with fresh_db.backend() as conn:
        conn.execute("update public.profiles set disabled = true where user_id = %s", [a.id])
    with fresh_db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=60)
    assert claimed is not None and claimed.id == tb.id


# --- 7. stopped_task_ids --------------------------------------------------------------------


def test_stopped_task_ids_returns_only_terminal_tasks(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    live = queued_task(fresh_db, user=user)
    cancelled = queued_task(fresh_db, user=user, status="cancelled")
    failed = queued_task(fresh_db, user=user, status="failed")
    with fresh_db.backend() as conn:
        got = store.stopped_task_ids(conn, [live.id, cancelled.id, failed.id])
        assert store.stopped_task_ids(conn, []) == []
    assert sorted(got) == sorted([cancelled.id, failed.id])


# --- 8. task_cost_budget --------------------------------------------------------------------


def budget(db: Database, task_id: uuid.UUID) -> float:
    with db.backend() as conn:
        return store.task_cost_budget(conn, task_id, AbuseLimits())


def test_budget_is_limited_by_the_day_when_that_is_smaller(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    with fresh_db.backend() as conn:
        conn.execute("update public.tasks set cost_usd = 0.4 where id = %s", [task.id])
    set_usage(fresh_db, user.id, cost=2.9)
    assert budget(fresh_db, task.id) == pytest.approx(0.1)


def test_budget_is_limited_by_the_task_when_that_is_smaller(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    with fresh_db.backend() as conn:
        conn.execute("update public.tasks set cost_usd = 0.2 where id = %s", [task.id])
    set_usage(fresh_db, user.id, cost=0.5)
    assert budget(fresh_db, task.id) == pytest.approx(0.8)


def test_budget_is_not_positive_when_the_day_is_spent(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    set_usage(fresh_db, user.id, cost=3.0)
    assert budget(fresh_db, task.id) <= 0


def test_budget_for_an_unknown_task_raises_not_found(fresh_db: Database) -> None:
    with pytest.raises(store.NotFound), fresh_db.backend() as conn:
        store.task_cost_budget(conn, uuid.uuid4(), AbuseLimits())
