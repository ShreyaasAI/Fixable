"""M10a T5: task delete and workspace sweep (m10a-contract §1.5, decision 10).

`DELETE /v1/tasks/{id}` removes a released, finished task (FK cascade) and keeps the audit row.
The worker clears `worker_id` and the lease when a runner exits (`store.release_task`; the
reaper does the same) and sweeps `workspaces_root` folders whose task no longer exists.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import test_m8_api
from m4_fakes import AutoDecider, queued_task, task_row
from test_m8_api import TODAY, Env, M8Client
from test_m8_worker import make_rig

from swf import store
from swf.db import Database
from swf.worker import Worker, WorkerConfig, orphan_workspaces

m8_client = test_m8_api.m8_client  # reuse the M8 app fixture


def _make_task(env: Env, user: Any, status: str = "completed") -> uuid.UUID:
    """A task through the API, with one row in each child table, forced to `status`."""
    created = env.post(user)
    assert created.status_code == 201, created.text
    task_id = uuid.UUID(created.json()["task"]["id"])
    with env.db.backend() as conn:
        store.create_approval(conn, task_id, "plan", {"plan": "x"})
        store.create_question(conn, task_id, "which file?")
        store.add_evidence(conn, task_id, kind="baseline", exit_code=1)
    env.sql("update tasks set status = %s where id = %s", [status, task_id])
    return task_id


def _child_counts(env: Env, task_id: uuid.UUID) -> dict[str, int]:
    out: dict[str, int] = {}
    with env.db.backend() as conn:
        for table in ("events", "approvals", "questions", "evidence"):
            row = conn.execute(
                f"select count(*) as n from public.{table} where task_id = %s",  # type: ignore[arg-type]
                [task_id],
            ).fetchone()
            assert row is not None
            out[table] = row["n"]
    return out


def delete(env: Env, user: Any, task_id: Any) -> Any:
    return env.client.delete(f"/v1/tasks/{task_id}", headers=env.auth(user))


# --- the route --------------------------------------------------------------------------------


def test_delete_returns_204_then_the_task_is_gone(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user)

    resp = delete(env, user, task_id)

    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    gone = env.client.get(f"/v1/tasks/{task_id}", headers=env.auth(user))
    assert (gone.status_code, gone.json()["detail"]) == (404, "not_found")
    assert env.client.get("/v1/tasks", headers=env.auth(user)).json() == []
    assert delete(env, user, task_id).status_code == 404  # a second delete


def test_delete_cascades_to_events_approvals_questions_and_evidence(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user)
    before = _child_counts(env, task_id)
    assert all(n > 0 for n in before.values()), before

    assert delete(env, user, task_id).status_code == 204

    assert _child_counts(env, task_id) == {
        "events": 0,
        "approvals": 0,
        "questions": 0,
        "evidence": 0,
    }


def test_delete_keeps_an_audit_row_with_the_task_id_as_target(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user, "failed")

    assert delete(env, user, task_id).status_code == 204

    rows = env.audit(user, "task.delete")
    assert len(rows) == 1
    assert rows[0]["target"] == str(task_id)
    assert rows[0]["payload"] == {"repo": "octo/widgets", "status": "failed"}
    assert len(env.audit(user, "task.create")) == 1  # earlier audit rows stay too


def test_delete_does_not_give_the_usage_back(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user)
    env.sql("update quotas_usage set cost_usd = 0.5 where user_id = %s", [user.id])

    def usage() -> Any:
        with env.db.backend() as conn:
            return conn.execute(
                "select tasks, cost_usd from quotas_usage where user_id = %s and day = %s",
                [user.id, TODAY],
            ).fetchone()

    before = usage()
    assert before is not None and before["tasks"] == 1

    assert delete(env, user, task_id).status_code == 204

    assert usage() == before


@pytest.mark.parametrize("status", ["queued", "preparing", "executing", "awaiting_input"])
def test_delete_of_a_non_terminal_task_is_409_task_not_finished(
    m8_client: M8Client, status: str
) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user, status)

    resp = delete(env, user, task_id)

    assert (resp.status_code, resp.json()["detail"]) == (409, "task_not_finished")
    assert env.tasks(user)[0]["id"] == task_id
    assert env.audit(user, "task.delete") == []


def test_delete_while_a_worker_still_holds_the_task_is_409_task_busy(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user, "cancelled")
    env.sql("update tasks set worker_id = 'w1' where id = %s", [task_id])

    resp = delete(env, user, task_id)

    assert (resp.status_code, resp.json()["detail"]) == (409, "task_busy")
    assert env.tasks(user)[0]["id"] == task_id

    env.sql("update tasks set worker_id = null where id = %s", [task_id])
    assert delete(env, user, task_id).status_code == 204


@pytest.mark.parametrize("status", ["rejected", "cancelled"])
def test_a_never_claimed_finished_task_can_be_deleted_at_once(
    m8_client: M8Client, status: str
) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user, status)

    assert delete(env, user, task_id).status_code == 204


def test_another_users_task_is_404_and_is_left_alone(m8_client: M8Client) -> None:
    env = m8_client()
    owner, other = env.user(), env.user()
    task_id = _make_task(env, owner)

    resp = delete(env, other, task_id)

    assert (resp.status_code, resp.json()["detail"]) == (404, "not_found")
    assert [t["id"] for t in env.tasks(owner)] == [task_id]
    assert env.audit(other, "task.delete") == []
    assert delete(env, other, uuid.uuid4()).status_code == 404


def test_delete_requires_authentication(m8_client: M8Client) -> None:
    env = m8_client()
    assert env.client.delete(f"/v1/tasks/{uuid.uuid4()}").status_code == 401


# --- store: release_task, interrupt_orphans, existing_task_ids ----------------------------------


def _hold(db: Database, task_id: uuid.UUID, status: str, worker_id: str | None = "w1") -> None:
    with db.backend() as conn:
        conn.execute(
            "update public.tasks set status = %s, worker_id = %s,"
            " lease_expires_at = now() + interval '1 minute' where id = %s",
            [status, worker_id, task_id],
        )


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_release_task_clears_worker_and_lease_of_a_finished_task(
    fresh_db: Database, status: str
) -> None:
    task = queued_task(fresh_db)
    _hold(fresh_db, task.id, status)

    with fresh_db.backend() as conn:
        store.release_task(conn, task.id, "w1")

    row = task_row(fresh_db, task.id)
    assert (row["worker_id"], row["lease_expires_at"]) == (None, None)
    assert row["status"] == status


def test_release_task_leaves_a_task_that_is_not_terminal(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    _hold(fresh_db, task.id, "executing")

    with fresh_db.backend() as conn:
        store.release_task(conn, task.id, "w1")

    row = task_row(fresh_db, task.id)
    assert row["worker_id"] == "w1"
    assert row["lease_expires_at"] is not None


def test_release_task_leaves_a_task_held_by_another_worker(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    _hold(fresh_db, task.id, "cancelled", worker_id="someone-else")

    with fresh_db.backend() as conn:
        store.release_task(conn, task.id, "w1")

    row = task_row(fresh_db, task.id)
    assert row["worker_id"] == "someone-else"
    assert row["lease_expires_at"] is not None


@pytest.mark.parametrize("restart", [False, True])
def test_interrupt_orphans_leaves_worker_id_and_lease_null(
    fresh_db: Database, restart: bool
) -> None:
    task = queued_task(fresh_db)
    _hold(fresh_db, task.id, "executing", worker_id="w1")
    with fresh_db.backend() as conn:
        conn.execute(
            "update public.tasks set lease_expires_at = now() - interval '1 minute' where id = %s",
            [task.id],
        )

    with fresh_db.backend() as conn:
        reaped = store.interrupt_orphans(conn, worker_id="w1" if restart else "w2", restart=restart)

    assert reaped == [(task.id, "w1")]  # the previous worker is still reported
    row = task_row(fresh_db, task.id)
    assert row["status"] == "interrupted"
    assert (row["worker_id"], row["lease_expires_at"]) == (None, None)


def _finished(db: Database, task_id: uuid.UUID, ago_seconds: int, worker_id: str = "w1") -> None:
    """A real terminal row: `transition_task` cleared the lease; the runner may still hold it."""
    with db.backend() as conn:
        conn.execute(
            "update public.tasks set status = 'cancelled', worker_id = %s,"
            " lease_expires_at = null, finished_at = now() - make_interval(secs => %s)"
            " where id = %s",
            [worker_id, ago_seconds, task_id],
        )


def test_a_just_cancelled_task_keeps_its_worker_while_the_runner_winds_down(
    fresh_db: Database,
) -> None:
    # Review finding 1: a terminal transition nulls the lease, so the reaper must not treat a
    # fresh terminal row as abandoned; its runner may still be writing (cost, last events).
    task = queued_task(fresh_db)
    _finished(fresh_db, task.id, ago_seconds=5)

    with fresh_db.backend() as conn:
        store.interrupt_orphans(conn, worker_id="w2", restart=False, lease_seconds=90)

    assert task_row(fresh_db, task.id)["worker_id"] == "w1"


def test_a_terminal_task_left_by_a_dead_worker_is_released_after_one_lease(
    fresh_db: Database,
) -> None:
    task = queued_task(fresh_db)
    _finished(fresh_db, task.id, ago_seconds=120)

    with fresh_db.backend() as conn:
        store.interrupt_orphans(conn, worker_id="w2", restart=False, lease_seconds=90)

    assert task_row(fresh_db, task.id)["worker_id"] is None


def test_a_reaped_task_can_then_be_deleted_by_its_owner(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    task_id = _make_task(env, user, "executing")
    env.sql(
        "update tasks set worker_id = 'w1', lease_expires_at = now() - interval '1 minute'"
        " where id = %s",
        [task_id],
    )
    with env.db.backend() as conn:
        store.interrupt_orphans(conn, worker_id="w2", restart=False)

    assert delete(env, user, task_id).status_code == 204


def test_existing_task_ids_returns_only_the_ids_that_have_a_row(fresh_db: Database) -> None:
    a, b = queued_task(fresh_db), queued_task(fresh_db)
    ghost = uuid.uuid4()

    with fresh_db.backend() as conn:
        assert store.existing_task_ids(conn, [a.id, ghost, b.id]) == {a.id, b.id}
        assert store.existing_task_ids(conn, []) == set()
        assert store.existing_task_ids(conn, {ghost}) == set()


# --- orphan_workspaces (pure) ------------------------------------------------------------------


def test_orphan_workspaces_returns_sorted_names_with_no_task_and_no_runner() -> None:
    gone_b, gone_a = uuid.UUID(int=2), uuid.UUID(int=1)
    kept_row, kept_run = uuid.uuid4(), uuid.uuid4()

    result = orphan_workspaces(
        [str(gone_b), str(kept_row), str(gone_a), str(kept_run)],
        existing={kept_row},
        running={kept_run},
    )

    assert result == sorted([str(gone_a), str(gone_b)])
    assert result == [str(gone_a), str(gone_b)]


def test_orphan_workspaces_ignores_names_that_are_not_uuids() -> None:
    names = ["lost+found", ".git", "pkgcache", "notes.txt", "1234", "", "..", "x" * 36]

    assert orphan_workspaces(names, existing=set(), running=set()) == []


def test_orphan_workspaces_accepts_any_iterable_and_handles_empty() -> None:
    gone = uuid.uuid4()
    assert orphan_workspaces(iter([str(gone)]), existing=set(), running=set()) == [str(gone)]
    assert orphan_workspaces([], existing=set(), running=set()) == []


# --- worker ------------------------------------------------------------------------------------


def test_sweep_seconds_defaults_to_sixty() -> None:
    assert WorkerConfig(worker_id="w1", workspaces_root=Path("x")).sweep_seconds == 60.0


def test_the_runner_releases_the_task_when_it_exits(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(
        fresh_db,
        tmp_path,
        env_error=True,
        agent={"poll_seconds": 0.02, "retry_backoff_seconds": []},
    )
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])

    with AutoDecider(fresh_db, answer="no"):  # the env error asks "retry?"; "no" ends it failed
        assert worker.run_once() == task.id

    row = task_row(fresh_db, task.id)
    assert row["status"] == "failed"
    assert (row["worker_id"], row["lease_expires_at"]) == (None, None)


def test_sweep_removes_orphan_workspaces_and_never_a_non_uuid_folder(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    root = rig.deps.config.workspaces_root
    root.mkdir(parents=True)
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])
    existing = queued_task(fresh_db)
    running = uuid.uuid4()
    worker.running[running] = object()  # type: ignore[assignment]  # only the key is read
    orphans = sorted(str(uuid.uuid4()) for _ in range(2))
    keep = [str(existing.id), str(running), "pkgcache", "lost+found", ".hidden"]
    for name in [*orphans, *keep]:
        (root / name).mkdir()
        (root / name / "file.txt").write_text("x", encoding="utf-8")

    removed = worker.sweep_workspaces()

    assert removed == orphans
    for name in orphans:
        assert not (root / name).exists()
    for name in keep:
        assert (root / name / "file.txt").exists(), name


def test_sweep_with_no_workspaces_root_is_a_no_op(fresh_db: Database, tmp_path: Path) -> None:
    rig = make_rig(fresh_db, tmp_path)
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])
    assert not rig.deps.config.workspaces_root.exists()

    assert worker.sweep_workspaces() == []
