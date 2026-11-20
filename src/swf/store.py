"""Repositories over the M1 schema (`supabase/migrations/`).

Conventions:
- Read functions take `user_id` and filter on it, and are called inside `Database.as_user`, so
  RLS is a second, independent filter.
- Mutating API functions run inside `Database.backend`, check ownership themselves, and write
  their audit row in the same transaction (plan §4: every mutating call is audited).
- Ownership failures raise `NotFound` (never reveal that another user's row exists).
- Worker-side functions (M4) run inside `Database.backend` with no user: the worker owns the
  task it claimed. Every status change goes through `transition_task` (state machine check +
  `status` event); every event gets the next gap-free `seq` from `tasks.event_seq`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, field_validator

from swf import states
from swf.db import Conn
from swf.preflight import Capacity, Limits, PreflightResult, Reservation, fits

TASK_STATUSES = (
    "created",
    "queued",
    "preparing",
    "investigating",
    "awaiting_plan_approval",
    "executing",
    "awaiting_command_approval",
    "awaiting_input",
    "verifying",
    "confirming",
    "opening_pr",
    "completed",
    "rejected",
    "failed",
    "interrupted",
    "cancelled",
)
TERMINAL_STATUSES = states.TERMINAL_STATUSES
# Statuses that hold a container (or are about to); they count against host capacity.
ACTIVE_STATUSES = (
    "preparing",
    "investigating",
    "awaiting_plan_approval",
    "executing",
    "awaiting_command_approval",
    "awaiting_input",
    "verifying",
    "confirming",
    "opening_pr",
)
# Reservation assumed for an active row without one: the per-task maximum.
_DEFAULT_RESERVATION_MB = 4096
_DEFAULT_RESERVATION_CPU = 1.0

ApprovalKind = Literal["plan", "command", "guard_flag"]
Decision = Literal["approve", "reject"]


class NotFound(Exception):
    pass


class Conflict(Exception):
    """The row exists but is not in a state that allows the change; message is the API code."""


class Profile(BaseModel):
    user_id: uuid.UUID
    github_login: str | None
    is_admin: bool
    disabled: bool


class Task(BaseModel):
    id: uuid.UUID
    repo: str
    base_branch: str
    description: str
    failing_test: str | None
    status: str
    phase: str | None
    reservation_mb: int | None
    reservation_cpu: float | None
    cost_usd: float
    pr_url: str | None
    preflight: PreflightResult | None
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    worker_id: str | None = None
    claimed_at: datetime | None = None
    finished_at: datetime | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None

    @field_validator("cost_usd", "reservation_cpu", mode="before")
    @classmethod
    def _decimal_to_float(cls, v: Any) -> Any:
        return float(v) if isinstance(v, Decimal) else v


class Approval(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    status: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    comment: str | None
    created_at: datetime


class Question(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    question: str
    answer: str | None
    answered_at: datetime | None
    created_at: datetime
    source: str = "agent"
    expired_at: datetime | None = None


class Event(BaseModel):
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


class Usage(BaseModel):
    day: date
    tasks: int
    cost_usd: float

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _decimal_to_float(cls, v: Any) -> Any:
        return float(v) if isinstance(v, Decimal) else v


def _jsonb(payload: dict[str, Any]) -> Jsonb:
    # Event payloads may carry uuids and datetimes; store them as strings.
    return Jsonb(payload, dumps=lambda obj: json.dumps(obj, default=str))


def _audit(
    conn: Conn, user_id: uuid.UUID, action: str, target: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        "insert into public.audit_log (user_id, action, target, payload) values (%s, %s, %s, %s)",
        [user_id, action, target, Jsonb(payload)],
    )


# --- profiles & tokens (backend) -----------------------------------------------------------


def ensure_profile(conn: Conn, user_id: uuid.UUID, github_login: str | None) -> Profile:
    row = conn.execute(
        "insert into public.profiles (user_id, github_login) values (%s, %s)"
        " on conflict (user_id) do update"
        " set github_login = coalesce(excluded.github_login, public.profiles.github_login)"
        " returning user_id, github_login, is_admin, disabled",
        [user_id, github_login],
    ).fetchone()
    assert row is not None
    return Profile.model_validate(row)


def store_github_token(
    conn: Conn,
    user_id: uuid.UUID,
    *,
    encrypted_token: str,
    scopes: tuple[str, ...],
    github_user_id: int,
) -> None:
    conn.execute(
        "insert into public.github_tokens (user_id, encrypted_token, scopes, github_user_id)"
        " values (%s, %s, %s, %s)"
        " on conflict (user_id) do update set encrypted_token = excluded.encrypted_token,"
        " scopes = excluded.scopes, github_user_id = excluded.github_user_id, updated_at = now()",
        [user_id, encrypted_token, list(scopes), github_user_id],
    )
    _audit(
        conn,
        user_id,
        "github_token.store",
        str(user_id),
        {"scopes": list(scopes), "github_user_id": github_user_id},
    )


def get_encrypted_github_token(conn: Conn, user_id: uuid.UUID) -> str | None:
    row = conn.execute(
        "select encrypted_token from public.github_tokens where user_id = %s", [user_id]
    ).fetchone()
    return None if row is None else str(row["encrypted_token"])


# --- reads (as_user) -----------------------------------------------------------------------


def get_profile(conn: Conn, user_id: uuid.UUID) -> Profile | None:
    row = conn.execute(
        "select user_id, github_login, is_admin, disabled from public.profiles where user_id = %s",
        [user_id],
    ).fetchone()
    return None if row is None else Profile.model_validate(row)


def get_usage(conn: Conn, user_id: uuid.UUID, day: date) -> Usage:
    row = conn.execute(
        "select day, tasks, cost_usd from public.quotas_usage where user_id = %s and day = %s",
        [user_id, day],
    ).fetchone()
    if row is None:
        return Usage(day=day, tasks=0, cost_usd=0.0)
    return Usage.model_validate(row)


def list_tasks(conn: Conn, user_id: uuid.UUID, *, limit: int = 50) -> list[Task]:
    rows = conn.execute(
        "select * from public.tasks where user_id = %s order by created_seq desc limit %s",
        [user_id, limit],
    ).fetchall()
    return [Task.model_validate(r) for r in rows]


def get_task(conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID) -> Task | None:
    row = conn.execute(
        "select * from public.tasks where id = %s and user_id = %s", [task_id, user_id]
    ).fetchone()
    return None if row is None else Task.model_validate(row)


# --- task mutations (backend, audited) ----------------------------------------------------


def create_task(
    conn: Conn,
    user_id: uuid.UUID,
    *,
    repo: str,
    base_branch: str,
    description: str,
    failing_test: str | None,
    day: date,
    status: str,
    reservation_mb: int | None,
    reservation_cpu: float | None,
    preflight: PreflightResult | None,
) -> Task:
    """A task rejected at pre-flight is recorded but not counted against the daily quota."""
    row = conn.execute(
        "insert into public.tasks (user_id, repo, base_branch, description, failing_test,"
        " status, reservation_mb, reservation_cpu, preflight)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning *",
        [
            user_id,
            repo,
            base_branch,
            description,
            failing_test,
            status,
            reservation_mb,
            reservation_cpu,
            None if preflight is None else Jsonb(preflight.model_dump(mode="json")),
        ],
    ).fetchone()
    assert row is not None
    task = Task.model_validate(row)
    append_event(conn, task.id, "status", {"from": None, "to": status})
    if status != "rejected":
        conn.execute(
            "insert into public.quotas_usage (user_id, day, tasks) values (%s, %s, 1)"
            " on conflict (user_id, day) do update set tasks = public.quotas_usage.tasks + 1",
            [user_id, day],
        )
    _audit(
        conn,
        user_id,
        "task.create",
        str(task.id),
        {
            "repo": repo,
            "base_branch": base_branch,
            "preflight": None if preflight is None else preflight.decision,
        },
    )
    return task


def capacity_snapshot(conn: Conn) -> Capacity:
    """Host-wide (all users) reservations of active tasks and of queued tasks (FIFO order)."""
    rows = conn.execute(
        "select status, coalesce(reservation_mb, %s) as mb, coalesce(reservation_cpu, %s) as cpu"
        " from public.tasks where status = any(%s) order by created_seq",
        [_DEFAULT_RESERVATION_MB, _DEFAULT_RESERVATION_CPU, [*ACTIVE_STATUSES, "queued"]],
    ).fetchall()
    active = tuple(
        Reservation(int(r["mb"]), float(r["cpu"])) for r in rows if r["status"] != "queued"
    )
    queued = tuple(
        Reservation(int(r["mb"]), float(r["cpu"])) for r in rows if r["status"] == "queued"
    )
    return Capacity(active=active, queued=queued)


def _lock_owned_task(conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID) -> str:
    """Lock the caller's task row and return its status; NotFound if not theirs."""
    row = conn.execute(
        "select status from public.tasks where id = %s and user_id = %s for update",
        [task_id, user_id],
    ).fetchone()
    if row is None:
        raise NotFound
    return str(row["status"])


def cancel_task(conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    status = _lock_owned_task(conn, user_id, task_id)
    try:
        states.check_transition(status, "cancelled")
    except (states.TaskFinished, states.InvalidTransition):
        raise Conflict("task_finished") from None
    _expire_pending(conn, task_id)
    task = transition_task(conn, task_id, "cancelled")
    _audit(conn, user_id, "task.cancel", str(task_id), {"from_status": status})
    return task


# --- approvals & questions ----------------------------------------------------------------


def create_approval(
    conn: Conn, task_id: uuid.UUID, kind: ApprovalKind | str, payload: dict[str, Any]
) -> Approval:
    """Worker side (M4): the agent requests a decision. At most one is pending per task."""
    row = conn.execute(
        "insert into public.approvals (task_id, kind, payload) values (%s, %s, %s) returning *",
        [task_id, kind, _jsonb(payload)],
    ).fetchone()
    assert row is not None
    approval = Approval.model_validate(row)
    append_event(
        conn,
        task_id,
        "approval_requested",
        {"approval_id": str(approval.id), "kind": approval.kind, "payload": approval.payload},
    )
    return approval


def decide_approval(
    conn: Conn,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    approval_id: uuid.UUID,
    *,
    decision: Decision,
    comment: str | None,
) -> Approval:
    """Submitter-only decision (spec §8)."""
    status = _lock_owned_task(conn, user_id, task_id)
    row = conn.execute(
        "select status, kind from public.approvals where id = %s and task_id = %s for update",
        [approval_id, task_id],
    ).fetchone()
    if row is None:
        raise NotFound
    if status in TERMINAL_STATUSES:
        raise Conflict("task_finished")
    if row["status"] != "pending":
        raise Conflict("already_decided")
    new_status = "approved" if decision == "approve" else "rejected"
    updated = conn.execute(
        "update public.approvals set status = %s, decided_by = %s, decided_at = now(),"
        " comment = %s where id = %s returning *",
        [new_status, user_id, comment, approval_id],
    ).fetchone()
    assert updated is not None
    _audit(
        conn,
        user_id,
        "approval.decide",
        str(approval_id),
        {"task_id": str(task_id), "kind": row["kind"], "decision": decision},
    )
    append_event(
        conn,
        task_id,
        "approval_decided",
        {
            "approval_id": str(approval_id),
            "kind": row["kind"],
            "decision": decision,
            "comment": comment,
        },
    )
    return Approval.model_validate(updated)


def create_question(
    conn: Conn,
    task_id: uuid.UUID,
    question: str,
    *,
    source: Literal["agent", "system"] = "agent",
) -> Question:
    """Worker side (M4): the agent's `ask_user`, or a system question (retries exhausted)."""
    row = conn.execute(
        "insert into public.questions (task_id, question, source) values (%s, %s, %s) returning *",
        [task_id, question, source],
    ).fetchone()
    assert row is not None
    q = Question.model_validate(row)
    append_event(
        conn,
        task_id,
        "question_asked",
        {"question_id": str(q.id), "question": question, "source": source},
    )
    return q


def answer_question(
    conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID, question_id: uuid.UUID, *, answer: str
) -> Question:
    status = _lock_owned_task(conn, user_id, task_id)
    row = conn.execute(
        "select answer, expired_at from public.questions where id = %s and task_id = %s for update",
        [question_id, task_id],
    ).fetchone()
    if row is None:
        raise NotFound
    if status in TERMINAL_STATUSES:
        raise Conflict("task_finished")
    if row["answer"] is not None or row["expired_at"] is not None:
        raise Conflict("already_answered")
    updated = conn.execute(
        "update public.questions set answer = %s, answered_at = now() where id = %s returning *",
        [answer, question_id],
    ).fetchone()
    assert updated is not None
    _audit(conn, user_id, "question.answer", str(question_id), {"task_id": str(task_id)})
    append_event(conn, task_id, "question_answered", {"question_id": str(question_id)})
    return Question.model_validate(updated)


# --- events --------------------------------------------------------------------------------


def append_event(conn: Conn, task_id: uuid.UUID, type: str, payload: dict[str, Any]) -> Event:
    """Append an event with the task's next seq (0-based, gap-free; the row lock serialises)."""
    row = conn.execute(
        "update public.tasks set event_seq = event_seq + 1 where id = %s"
        " returning event_seq - 1 as seq",
        [task_id],
    ).fetchone()
    if row is None:
        raise NotFound
    ev = conn.execute(
        "insert into public.events (task_id, seq, type, payload) values (%s, %s, %s, %s)"
        " returning seq, type, payload, created_at",
        [task_id, row["seq"], type, _jsonb(payload)],
    ).fetchone()
    assert ev is not None
    return Event.model_validate(ev)


def list_events(
    conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID, *, after_seq: int = -1, limit: int = 500
) -> list[Event]:
    """Events of the caller's task with seq > `after_seq`, in order (as_user)."""
    rows = conn.execute(
        "select e.seq, e.type, e.payload, e.created_at from public.events e"
        " join public.tasks t on t.id = e.task_id"
        " where e.task_id = %s and t.user_id = %s and e.seq > %s order by e.seq limit %s",
        [task_id, user_id, after_seq, limit],
    ).fetchall()
    return [Event.model_validate(r) for r in rows]


# --- worker side (backend) ----------------------------------------------------------------


def transition_task(
    conn: Conn,
    task_id: uuid.UUID,
    to_status: str,
    *,
    phase: str | None = None,
    error: str | None = None,
) -> Task:
    """Validated status change plus its `status` event.

    Raises `states.TaskFinished` if the row is already terminal and `states.InvalidTransition`
    for a move the table does not allow. A terminal status sets `finished_at` and clears the
    lease.
    """
    row = conn.execute(
        "select status from public.tasks where id = %s for update", [task_id]
    ).fetchone()
    if row is None:
        raise NotFound
    from_status = str(row["status"])
    states.check_transition(from_status, to_status)
    terminal = to_status in TERMINAL_STATUSES
    updated = conn.execute(
        "update public.tasks set status = %s, phase = coalesce(%s, phase),"
        " error = coalesce(%s, error),"
        " finished_at = case when %s then now() else finished_at end,"
        " lease_expires_at = case when %s then null else lease_expires_at end"
        " where id = %s returning *",
        [to_status, phase, error, terminal, terminal, task_id],
    ).fetchone()
    assert updated is not None
    append_event(
        conn,
        task_id,
        "status",
        {"from": from_status, "to": to_status, "phase": phase, "error": error},
    )
    return Task.model_validate(updated)


def get_task_status(conn: Conn, task_id: uuid.UUID) -> str | None:
    row = conn.execute("select status from public.tasks where id = %s", [task_id]).fetchone()
    return None if row is None else str(row["status"])


def get_approval(conn: Conn, approval_id: uuid.UUID) -> Approval | None:
    row = conn.execute("select * from public.approvals where id = %s", [approval_id]).fetchone()
    return None if row is None else Approval.model_validate(row)


def get_question(conn: Conn, question_id: uuid.UUID) -> Question | None:
    row = conn.execute("select * from public.questions where id = %s", [question_id]).fetchone()
    return None if row is None else Question.model_validate(row)


def expire_approval(conn: Conn, approval_id: uuid.UUID) -> None:
    """Pending -> expired (idle timeout or task end); a decided approval is left alone."""
    row = conn.execute(
        "update public.approvals set status = 'expired', decided_at = now()"
        " where id = %s and status = 'pending' returning task_id",
        [approval_id],
    ).fetchone()
    if row is not None:
        append_event(conn, row["task_id"], "approval_expired", {"approval_id": str(approval_id)})


def expire_question(conn: Conn, question_id: uuid.UUID) -> None:
    """Open -> expired; an answered question is left alone."""
    row = conn.execute(
        "update public.questions set expired_at = now()"
        " where id = %s and answer is null and expired_at is null returning task_id",
        [question_id],
    ).fetchone()
    if row is not None:
        append_event(conn, row["task_id"], "question_expired", {"question_id": str(question_id)})


def _expire_pending(conn: Conn, task_id: uuid.UUID) -> None:
    """Expire the task's pending approvals and open questions (the task is ending)."""
    approvals = conn.execute(
        "select id from public.approvals where task_id = %s and status = 'pending'", [task_id]
    ).fetchall()
    for a in approvals:
        expire_approval(conn, a["id"])
    questions = conn.execute(
        "select id from public.questions"
        " where task_id = %s and answer is null and expired_at is null",
        [task_id],
    ).fetchall()
    for q in questions:
        expire_question(conn, q["id"])


def add_task_cost(conn: Conn, task_id: uuid.UUID, usd: float) -> None:
    """Add model spend to the task and to the owner's usage for the task's creation day."""
    if usd <= 0:
        return
    row = conn.execute(
        "update public.tasks set cost_usd = cost_usd + %s where id = %s"
        " returning user_id, (created_at at time zone 'UTC')::date as day",
        [usd, task_id],
    ).fetchone()
    if row is None:
        raise NotFound
    conn.execute(
        "insert into public.quotas_usage (user_id, day, cost_usd) values (%s, %s, %s)"
        " on conflict (user_id, day)"
        " do update set cost_usd = public.quotas_usage.cost_usd + excluded.cost_usd",
        [row["user_id"], row["day"], usd],
    )


def claim_next_task(
    conn: Conn,
    *,
    worker_id: str,
    lease_seconds: int,
    limits: Limits = Limits(),  # noqa: B008 - frozen dataclass, safe as a default
) -> Task | None:
    """Admit the FIFO head if it fits next to the active tasks (m4-contract §8).

    The advisory lock serialises admission across workers; a head that does not fit blocks the
    queue (no skipping, so no starvation). The lease is set from the DB clock.
    """
    conn.execute("select pg_advisory_xact_lock(hashtext('swf.admission'))")
    head = conn.execute(
        "select id, coalesce(reservation_mb, %s) as mb, coalesce(reservation_cpu, %s) as cpu"
        " from public.tasks where status = 'queued' order by created_seq limit 1"
        " for update skip locked",
        [_DEFAULT_RESERVATION_MB, _DEFAULT_RESERVATION_CPU],
    ).fetchone()
    if head is None:
        return None
    rows = conn.execute(
        "select coalesce(reservation_mb, %s) as mb, coalesce(reservation_cpu, %s) as cpu"
        " from public.tasks where status = any(%s)",
        [_DEFAULT_RESERVATION_MB, _DEFAULT_RESERVATION_CPU, list(ACTIVE_STATUSES)],
    ).fetchall()
    active = [Reservation(int(r["mb"]), float(r["cpu"])) for r in rows]
    if not fits(active, Reservation(int(head["mb"]), float(head["cpu"])), limits):
        return None
    conn.execute(
        "update public.tasks set worker_id = %s, claimed_at = now(),"
        " lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now()"
        " where id = %s",
        [worker_id, lease_seconds, head["id"]],
    )
    return transition_task(conn, head["id"], "preparing", phase="prepare")


def renew_leases(
    conn: Conn, *, worker_id: str, task_ids: Collection[uuid.UUID], lease_seconds: int
) -> list[uuid.UUID]:
    """Extend this worker's leases; ids not returned are lost (reaped, finished or moved)."""
    if not task_ids:
        return []
    rows = conn.execute(
        "update public.tasks set lease_expires_at = now() + make_interval(secs => %s),"
        " heartbeat_at = now()"
        " where id = any(%s) and worker_id = %s and status = any(%s) returning id",
        [lease_seconds, list(task_ids), worker_id, list(ACTIVE_STATUSES)],
    ).fetchall()
    return [r["id"] for r in rows]


def interrupt_orphans(
    conn: Conn, *, worker_id: str, restart: bool
) -> list[tuple[uuid.UUID, str | None]]:
    """Interrupt active tasks whose lease expired (any worker) and, on `restart`, this worker's
    own tasks. Returns (task id, previous worker id); their pendings are expired."""
    rows = conn.execute(
        "select id, worker_id from public.tasks where status = any(%s)"
        " and (lease_expires_at is null or lease_expires_at < now()"
        " or (%s and worker_id = %s))"
        " order by created_seq for update skip locked",
        [list(ACTIVE_STATUSES), restart, worker_id],
    ).fetchall()
    reaped: list[tuple[uuid.UUID, str | None]] = []
    for r in rows:
        previous = r["worker_id"]
        own = restart and previous == worker_id
        _expire_pending(conn, r["id"])
        transition_task(
            conn, r["id"], "interrupted", error="worker_restarted" if own else "lease_expired"
        )
        reaped.append((r["id"], previous))
    return reaped


def active_task_ids(conn: Conn) -> list[str]:
    rows = conn.execute(
        "select id from public.tasks where status = any(%s) order by created_seq",
        [list(ACTIVE_STATUSES)],
    ).fetchall()
    return [str(r["id"]) for r in rows]
