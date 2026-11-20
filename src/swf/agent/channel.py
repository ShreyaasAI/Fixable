"""The agent's line to the task row, approvals, questions and events (m4-contract §5.3).

`TaskChannel` is what `WebAgent` and `TaskRunner` need; `PgTaskChannel` implements it with one
backend transaction per call. Waits poll (`wait_for`) and are bounded by the idle timeout.
The agent's exit exceptions live here because `wait_for` raises two of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from minisweagent.exceptions import InterruptAgentFlow

from swf import states, store
from swf.db import Database

# --- exit exceptions -----------------------------------------------------------------------


class AgentExit(InterruptAgentFlow):
    """Ends `WebAgent.run`: carries one `{"role": "exit", ...}` message."""

    exit_status = "Exit"

    def __init__(
        self, content: str | None = None, *, exit_status: str | None = None, submission: str = ""
    ) -> None:
        status = exit_status or self.exit_status
        self.status = status
        super().__init__(
            {
                "role": "exit",
                "content": status if content is None else content,
                "extra": {"exit_status": status, "submission": submission},
            }
        )


class TaskSubmitted(AgentExit):
    exit_status = "Submitted"

    def __init__(self, summary: str = "") -> None:
        super().__init__(summary, submission=summary)


class TaskStopped(AgentExit):
    """The task row is terminal (cancelled, interrupted) or the runner was stopped."""

    exit_status = "Stopped"


class WaitTimedOut(AgentExit):
    """An approval/question waited past the idle timeout ("ApprovalTimeout"/"QuestionTimeout")."""

    exit_status = "ApprovalTimeout"


class RetriesExhausted(AgentExit):
    exit_status = "RetriesExhausted"


class SetupForbidden(AgentExit):
    exit_status = "SetupForbidden"


# --- channel -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalOutcome:
    status: Literal["approved", "rejected", "expired"]
    comment: str | None


class TaskChannel(Protocol):
    task_id: uuid.UUID

    def status(self) -> str: ...

    def transition(
        self, to_status: str, *, phase: str | None = None, error: str | None = None
    ) -> None: ...

    def emit(self, type: str, payload: dict[str, Any]) -> None: ...

    def request_approval(self, kind: str, payload: dict[str, Any]) -> uuid.UUID: ...

    def approval(self, approval_id: uuid.UUID) -> ApprovalOutcome | None: ...

    def expire_approval(self, approval_id: uuid.UUID) -> None: ...

    def ask(self, question: str, *, source: Literal["agent", "system"]) -> uuid.UUID: ...

    def answer(self, question_id: uuid.UUID) -> str | None: ...

    def expire_question(self, question_id: uuid.UUID) -> None: ...

    def add_cost(self, usd: float) -> None: ...


class PgTaskChannel(TaskChannel):
    """`TaskChannel` over the store; one backend transaction per call."""

    def __init__(self, db: Database, task_id: uuid.UUID) -> None:
        self.db = db
        self.task_id = task_id

    def status(self) -> str:
        with self.db.backend() as conn:
            status = store.get_task_status(conn, self.task_id)
        if status is None:
            raise store.NotFound
        return status

    def transition(
        self, to_status: str, *, phase: str | None = None, error: str | None = None
    ) -> None:
        with self.db.backend() as conn:
            store.transition_task(conn, self.task_id, to_status, phase=phase, error=error)

    def emit(self, type: str, payload: dict[str, Any]) -> None:
        with self.db.backend() as conn:
            store.append_event(conn, self.task_id, type, payload)

    def request_approval(self, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        with self.db.backend() as conn:
            return store.create_approval(conn, self.task_id, kind, payload).id

    def approval(self, approval_id: uuid.UUID) -> ApprovalOutcome | None:
        with self.db.backend() as conn:
            row = store.get_approval(conn, approval_id)
        if row is None or row.status == "pending":
            return None
        status: Literal["approved", "rejected", "expired"]
        if row.status == "approved":
            status = "approved"
        elif row.status == "rejected":
            status = "rejected"
        else:
            status = "expired"
        return ApprovalOutcome(status=status, comment=row.comment)

    def expire_approval(self, approval_id: uuid.UUID) -> None:
        with self.db.backend() as conn:
            store.expire_approval(conn, approval_id)

    def ask(self, question: str, *, source: Literal["agent", "system"]) -> uuid.UUID:
        with self.db.backend() as conn:
            return store.create_question(conn, self.task_id, question, source=source).id

    def answer(self, question_id: uuid.UUID) -> str | None:
        with self.db.backend() as conn:
            row = store.get_question(conn, question_id)
        return None if row is None else row.answer

    def expire_question(self, question_id: uuid.UUID) -> None:
        with self.db.backend() as conn:
            store.expire_question(conn, question_id)

    def add_cost(self, usd: float) -> None:
        if usd > 0:
            with self.db.backend() as conn:
                store.add_task_cost(conn, self.task_id, usd)


# --- waiting -------------------------------------------------------------------------------


def wait_for[T](
    poll: Callable[[], T | None],
    *,
    channel: TaskChannel,
    timeout_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    on_timeout: Callable[[], None],
    timeout_status: str = "ApprovalTimeout",
) -> T:
    """Poll until `poll()` returns a value; a terminal task -> `TaskStopped`; past the
    deadline -> `on_timeout()` then `WaitTimedOut(exit_status=timeout_status)`."""
    deadline = clock() + timeout_seconds
    while True:
        if states.is_terminal(channel.status()):
            raise TaskStopped()
        value = poll()
        if value is not None:
            return value
        if clock() >= deadline:
            on_timeout()
            raise WaitTimedOut(exit_status=timeout_status)
        sleep(poll_seconds)


@dataclass(frozen=True)
class WaitSettings:
    timeout_seconds: float
    poll_seconds: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def ask_and_wait(
    channel: TaskChannel,
    question: str,
    *,
    source: Literal["agent", "system"],
    back_to: str,
    wait: WaitSettings,
) -> str:
    """Ask, move the task to `awaiting_input`, wait for the answer, move back to `back_to`."""
    question_id = channel.ask(question, source=source)
    channel.transition("awaiting_input")
    answer = wait_for(
        lambda: channel.answer(question_id),
        channel=channel,
        timeout_seconds=wait.timeout_seconds,
        poll_seconds=wait.poll_seconds,
        clock=wait.clock,
        sleep=wait.sleep,
        on_timeout=lambda: channel.expire_question(question_id),
        timeout_status="QuestionTimeout",
    )
    channel.transition(back_to)
    return answer


def retry_question(step: str, attempts: int, error: str) -> str:
    return (
        f"The {step} step failed {attempts} times. Last error: {error[:1000]}\n"
        f"Answer 'retry' to try {attempts} more times; any other answer fails the task."
    )
