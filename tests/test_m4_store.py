"""M4 additions to `swf.store` (m4-contract §3, §4, A2): transitions, events, expiring
approvals/questions, costs, leases. Runs against a private database clone (`fresh_db`)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import FakeUser
from m4_fakes import make_db_user, queued_task, task_row

from swf import states, store
from swf.db import Database

pytest_plugins = ["m4_fakes"]

AuthFn = Callable[..., dict[str, str]]


def owner_of(db: Database, task: store.Task) -> FakeUser:
    """The FakeUser that owns `task` (so it can read through RLS)."""
    row = task_row(db, task.id)
    return FakeUser(id=row["user_id"], github_id=1, login="owner")


def new_task(db: Database, **kwargs: Any) -> tuple[FakeUser, store.Task]:
    user = make_db_user(db)
    return user, queued_task(db, user=user, **kwargs)


def events_of(db: Database, user: FakeUser, task: store.Task, **kwargs: Any) -> list[store.Event]:
    with db.as_user(user.claims()) as conn:
        return store.list_events(conn, user.id, task.id, **kwargs)


def move(db: Database, task_id: uuid.UUID, to: str, **kwargs: Any) -> store.Task:
    with db.backend() as conn:
        return store.transition_task(conn, task_id, to, **kwargs)


# --- transition_task ------------------------------------------------------------------------


def test_create_task_records_the_first_status_event(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    (event,) = events_of(fresh_db, user, task)
    assert event.seq == 0
    assert event.type == "status"
    assert event.payload["from"] is None
    assert event.payload["to"] == "queued"
    assert isinstance(event.created_at, datetime)


def test_transition_task_updates_the_row_and_emits_a_status_event(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    updated = move(fresh_db, task.id, "preparing", phase="prepare")
    assert updated.status == "preparing"
    assert updated.phase == "prepare"
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "status"
    assert last.payload == {"from": "queued", "to": "preparing", "phase": "prepare", "error": None}


def test_transition_task_rejects_a_move_the_table_does_not_allow(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with pytest.raises(states.InvalidTransition):
        move(fresh_db, task.id, "executing")
    assert task_row(fresh_db, task.id)["status"] == "queued"
    assert len(events_of(fresh_db, user, task)) == 1  # nothing was emitted


def test_a_terminal_transition_sets_finished_at_and_error(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    row = task_row(fresh_db, task.id)
    assert row["finished_at"] is None
    assert row["error"] is None
    done = move(fresh_db, task.id, "failed", error="retries_exhausted")
    assert done.status == "failed"
    assert done.error == "retries_exhausted"
    assert done.finished_at is not None
    assert done.phase == "prepare"  # an omitted phase is left alone


def test_a_finished_task_cannot_move_again(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    move(fresh_db, task.id, "cancelled")
    with pytest.raises(states.TaskFinished):
        move(fresh_db, task.id, "preparing")


def test_the_phase_can_change_with_the_status(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    move(fresh_db, task.id, "investigating", phase="investigate")
    assert move(fresh_db, task.id, "awaiting_plan_approval", phase="plan").phase == "plan"


def test_a_terminal_transition_clears_the_lease(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert task_row(fresh_db, task.id)["lease_expires_at"] is not None
    move(fresh_db, task.id, "failed", error="x")
    assert task_row(fresh_db, task.id)["lease_expires_at"] is None


# --- events ---------------------------------------------------------------------------------


def test_events_are_numbered_from_zero_without_gaps_under_concurrent_writers(
    fresh_db: Database,
) -> None:
    user, task = new_task(fresh_db)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        try:
            barrier.wait()
            for n in range(50):
                with fresh_db.backend() as conn:
                    store.append_event(conn, task.id, "note", {"who": name, "n": n})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    events = events_of(fresh_db, user, task, limit=1000)
    assert [e.seq for e in events] == list(range(101))  # the creation event plus 2 x 50
    notes = [e.payload for e in events if e.type == "note"]
    assert len({(p["who"], p["n"]) for p in notes}) == 100
    for who in ("a", "b"):
        assert [p["n"] for p in notes if p["who"] == who] == list(range(50))  # in order per writer


def test_append_event_returns_the_event(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        event = store.append_event(conn, task.id, "note", {"k": "v"})
    assert (event.seq, event.type, event.payload) == (1, "note", {"k": "v"})


def test_list_events_orders_resumes_and_limits(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        for n in range(5):
            store.append_event(conn, task.id, "note", {"n": n})
    all_events = events_of(fresh_db, user, task)
    assert [e.seq for e in all_events] == [0, 1, 2, 3, 4, 5]
    assert [e.seq for e in events_of(fresh_db, user, task, after_seq=3)] == [4, 5]
    assert [e.seq for e in events_of(fresh_db, user, task, after_seq=-1, limit=2)] == [0, 1]
    assert events_of(fresh_db, user, task, after_seq=5) == []


def test_another_user_sees_no_events(fresh_db: Database) -> None:
    owner, task = new_task(fresh_db)
    other = make_db_user(fresh_db)
    assert events_of(fresh_db, other, task) == []
    with fresh_db.as_user(other.claims()) as conn:  # RLS alone, without the user_id filter
        row = conn.execute(
            "select count(*) as n from public.events where task_id = %s", [task.id]
        ).fetchone()
    assert row is not None
    assert row["n"] == 0
    assert len(events_of(fresh_db, owner, task)) == 1


# --- approvals ------------------------------------------------------------------------------


def test_create_approval_emits_approval_requested(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {"summary": "s"})
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "approval_requested"
    assert last.payload == {
        "approval_id": str(approval.id),
        "kind": "plan",
        "payload": {"summary": "s"},
    }


def test_only_one_approval_can_be_pending_per_task(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        first = store.create_approval(conn, task.id, "plan", {})
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_db.backend() as conn:
        store.create_approval(conn, task.id, "command", {})
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        store.decide_approval(conn, user.id, task.id, first.id, decision="approve", comment=None)
        second = store.create_approval(conn, task.id, "command", {})  # allowed once decided
    assert second.status == "pending"


def test_decide_approval_emits_approval_decided(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "command", {"command": "touch x"})
        decided = store.decide_approval(
            conn, user.id, task.id, approval.id, decision="reject", comment="no"
        )
    assert decided.status == "rejected"
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "approval_decided"
    assert last.payload == {
        "approval_id": str(approval.id),
        "kind": "command",
        "decision": "reject",
        "comment": "no",
    }


def test_expire_approval_marks_it_and_emits_an_event(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {})
        store.expire_approval(conn, approval.id)
        expired = store.get_approval(conn, approval.id)
    assert expired is not None
    assert expired.status == "expired"
    assert expired.decided_at is not None
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "approval_expired"
    assert last.payload["approval_id"] == str(approval.id)
    with fresh_db.backend() as conn:  # an expired approval no longer blocks a new request
        assert store.create_approval(conn, task.id, "plan", {}).status == "pending"


def test_deciding_an_expired_approval_conflicts(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {})
        store.expire_approval(conn, approval.id)
    with pytest.raises(store.Conflict, match="already_decided"), fresh_db.backend() as conn:
        store.decide_approval(conn, user.id, task.id, approval.id, decision="approve", comment=None)


def test_the_api_answers_409_when_deciding_an_expired_approval(
    fresh_db: Database, fresh_client: TestClient, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {})
        store.expire_approval(conn, approval.id)
    response = fresh_client.post(
        f"/v1/tasks/{task.id}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers=auth(user),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already_decided"


def test_get_approval_and_get_question_return_none_for_unknown_ids(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        assert store.get_approval(conn, uuid.uuid4()) is None
        assert store.get_question(conn, uuid.uuid4()) is None


# --- questions ------------------------------------------------------------------------------


def test_create_question_emits_question_asked_and_records_the_source(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        agent_q = store.create_question(conn, task.id, "Which size?")
        assert agent_q.source == "agent"
        assert agent_q.expired_at is None
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "question_asked"
    assert last.payload == {
        "question_id": str(agent_q.id),
        "question": "Which size?",
        "source": "agent",
    }
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        store.answer_question(conn, user.id, task.id, agent_q.id, answer="10")
        system_q = store.create_question(conn, task.id, "Retry?", source="system")
    assert system_q.source == "system"


def test_only_one_question_can_be_open_per_task(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        store.create_question(conn, task.id, "one?")
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_db.backend() as conn:
        store.create_question(conn, task.id, "two?")


def test_answer_question_emits_question_answered(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        question = store.create_question(conn, task.id, "Which size?")
        answered = store.answer_question(conn, user.id, task.id, question.id, answer="10")
        fetched = store.get_question(conn, question.id)
    assert answered.answer == "10"
    assert fetched is not None
    assert fetched.answer == "10"
    last = events_of(fresh_db, user, task)[-1]
    assert (last.type, last.payload) == ("question_answered", {"question_id": str(question.id)})


def test_expire_question_marks_it_and_blocks_answers(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        question = store.create_question(conn, task.id, "Which size?")
        store.expire_question(conn, question.id)
        expired = store.get_question(conn, question.id)
    assert expired is not None
    assert expired.expired_at is not None
    assert expired.answer is None
    assert events_of(fresh_db, user, task)[-1].type == "question_expired"
    with pytest.raises(store.Conflict, match="already_answered"), fresh_db.backend() as conn:
        store.answer_question(conn, user.id, task.id, question.id, answer="10")
    with fresh_db.backend() as conn:  # no longer open, so a new question is fine
        assert store.create_question(conn, task.id, "again?").answer is None


def test_a_question_cannot_be_both_answered_and_expired(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        question = store.create_question(conn, task.id, "Which size?")
    with pytest.raises(psycopg.errors.CheckViolation), fresh_db.backend() as conn:
        conn.execute(
            "update public.questions set answer = 'x', answered_at = now(), expired_at = now()"
            " where id = %s",
            [question.id],
        )


# --- cancel ---------------------------------------------------------------------------------


def test_cancel_emits_status_and_expires_pending_requests(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    move(fresh_db, task.id, "preparing", phase="prepare")
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {})
        question = store.create_question(conn, task.id, "q?")
        cancelled = store.cancel_task(conn, user.id, task.id)
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None
    with fresh_db.backend() as conn:
        a, q = store.get_approval(conn, approval.id), store.get_question(conn, question.id)
    assert a is not None
    assert a.status == "expired"
    assert q is not None
    assert q.expired_at is not None
    last = events_of(fresh_db, user, task)[-1]
    assert last.type == "status"
    assert last.payload["to"] == "cancelled"
    assert last.payload["from"] == "preparing"


def test_cancelling_a_finished_task_conflicts(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)
    with pytest.raises(store.Conflict, match="task_finished"), fresh_db.backend() as conn:
        store.cancel_task(conn, user.id, task.id)


# --- cost -----------------------------------------------------------------------------------


def test_add_task_cost_accumulates_on_the_task_and_the_daily_usage(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        store.add_task_cost(conn, task.id, 0.25)
        store.add_task_cost(conn, task.id, 0.25)
    assert float(task_row(fresh_db, task.id)["cost_usd"]) == pytest.approx(0.5)
    day = task.created_at.astimezone(UTC).date()
    with fresh_db.as_user(user.claims()) as conn:
        usage = store.get_usage(conn, user.id, day)
    assert usage.cost_usd == pytest.approx(0.5)
    assert usage.tasks == 1


# --- leases (A2) ----------------------------------------------------------------------------


def test_renew_leases_returns_only_the_ids_it_renewed(fresh_db: Database) -> None:
    _, mine = new_task(fresh_db)
    _, theirs = new_task(fresh_db)
    with fresh_db.backend() as conn:
        a = store.claim_next_task(conn, worker_id="w1", lease_seconds=10)
        b = store.claim_next_task(conn, worker_id="w2", lease_seconds=10)
    assert a is not None
    assert b is not None
    assert {a.id, b.id} == {mine.id, theirs.id}
    ids = [mine.id, theirs.id, uuid.uuid4()]
    with fresh_db.backend() as conn:
        renewed = store.renew_leases(conn, worker_id="w1", task_ids=ids, lease_seconds=1000)
    assert renewed == [a.id]  # not another worker's row, not an unknown id
    assert (
        task_row(fresh_db, b.id)["lease_expires_at"] < task_row(fresh_db, a.id)["lease_expires_at"]
    )


def test_renew_leases_skips_tasks_that_are_no_longer_active(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=10)
    assert claimed is not None
    move(fresh_db, task.id, "interrupted", error="lease_expired")
    with fresh_db.backend() as conn:
        assert store.renew_leases(conn, worker_id="w1", task_ids=[task.id], lease_seconds=10) == []


def test_renew_leases_with_no_ids_is_a_no_op(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        assert store.renew_leases(conn, worker_id="w1", task_ids=[], lease_seconds=10) == []


@pytest.mark.parametrize("good", ["host-1", "a.b_c-D", "w" * 64])
def test_worker_id_accepts_the_documented_characters(fresh_db: Database, good: str) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        conn.execute("update public.tasks set worker_id = %s where id = %s", [good, task.id])


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "w" * 65, "", "slash/x"])
def test_worker_id_check_constraint_rejects_other_values(fresh_db: Database, bad: str) -> None:
    _, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation), fresh_db.backend() as conn:
        conn.execute("update public.tasks set worker_id = %s where id = %s", [bad, task.id])


def test_task_model_exposes_the_new_columns(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    assert task.error is None
    assert task.worker_id is None
    assert task.claimed_at is None
    assert task.finished_at is None
    with fresh_db.as_user(user.claims()) as conn:
        fetched = store.get_task(conn, user.id, task.id)
    assert fetched is not None
    assert fetched.error is None
