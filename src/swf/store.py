"""Repositories over the M1 schema (`supabase/migrations/`).

Conventions:
- Read functions take `user_id` and filter on it, and are called inside `Database.as_user`, so
  RLS is a second, independent filter.
- Mutating API functions run inside `Database.backend`, check ownership themselves, and write
  their audit row in the same transaction (plan §4: every mutating call is audited).
- Ownership failures raise `NotFound` (never reveal that another user's row exists).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, field_validator

from swf.db import Conn

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
TERMINAL_STATUSES = frozenset({"completed", "rejected", "failed", "interrupted", "cancelled"})

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
    created_at: datetime
    updated_at: datetime

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


class Usage(BaseModel):
    day: date
    tasks: int
    cost_usd: float

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _decimal_to_float(cls, v: Any) -> Any:
        return float(v) if isinstance(v, Decimal) else v


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
        "select * from public.tasks where user_id = %s order by created_at desc, id limit %s",
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
) -> Task:
    row = conn.execute(
        "insert into public.tasks (user_id, repo, base_branch, description, failing_test)"
        " values (%s, %s, %s, %s, %s) returning *",
        [user_id, repo, base_branch, description, failing_test],
    ).fetchone()
    assert row is not None
    task = Task.model_validate(row)
    conn.execute(
        "insert into public.quotas_usage (user_id, day, tasks) values (%s, %s, 1)"
        " on conflict (user_id, day) do update set tasks = public.quotas_usage.tasks + 1",
        [user_id, day],
    )
    _audit(conn, user_id, "task.create", str(task.id), {"repo": repo, "base_branch": base_branch})
    return task


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
    if status in TERMINAL_STATUSES:
        raise Conflict("task_finished")
    row = conn.execute(
        "update public.tasks set status = 'cancelled' where id = %s returning *", [task_id]
    ).fetchone()
    assert row is not None
    _audit(conn, user_id, "task.cancel", str(task_id), {"from_status": status})
    return Task.model_validate(row)


# --- approvals & questions ----------------------------------------------------------------


def create_approval(
    conn: Conn, task_id: uuid.UUID, kind: ApprovalKind | str, payload: dict[str, Any]
) -> Approval:
    """Worker side (M4): the agent requests a decision."""
    row = conn.execute(
        "insert into public.approvals (task_id, kind, payload) values (%s, %s, %s) returning *",
        [task_id, kind, Jsonb(payload)],
    ).fetchone()
    assert row is not None
    return Approval.model_validate(row)


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
    return Approval.model_validate(updated)


def create_question(conn: Conn, task_id: uuid.UUID, question: str) -> Question:
    """Worker side (M4): the agent's `ask_user`."""
    row = conn.execute(
        "insert into public.questions (task_id, question) values (%s, %s) returning *",
        [task_id, question],
    ).fetchone()
    assert row is not None
    return Question.model_validate(row)


def answer_question(
    conn: Conn, user_id: uuid.UUID, task_id: uuid.UUID, question_id: uuid.UUID, *, answer: str
) -> Question:
    status = _lock_owned_task(conn, user_id, task_id)
    row = conn.execute(
        "select answer from public.questions where id = %s and task_id = %s for update",
        [question_id, task_id],
    ).fetchone()
    if row is None:
        raise NotFound
    if status in TERMINAL_STATUSES:
        raise Conflict("task_finished")
    if row["answer"] is not None:
        raise Conflict("already_answered")
    updated = conn.execute(
        "update public.questions set answer = %s, answered_at = now() where id = %s returning *",
        [answer, question_id],
    ).fetchone()
    assert updated is not None
    _audit(conn, user_id, "question.answer", str(question_id), {"task_id": str(task_id)})
    return Question.model_validate(updated)
