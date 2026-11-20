"""Task state machine (m4-contract §2). Pure: no I/O."""

from __future__ import annotations

PHASE_STATUS: dict[str, str] = {
    "prepare": "preparing",
    "investigate": "investigating",
    "plan": "awaiting_plan_approval",
    "execute": "executing",
    "verify": "verifying",
    "confirm": "confirming",
    "pr": "opening_pr",
}

WAITING_STATUSES = frozenset(
    {"awaiting_plan_approval", "awaiting_command_approval", "awaiting_input"}
)
TERMINAL_STATUSES = frozenset({"completed", "rejected", "failed", "interrupted", "cancelled"})

_X = frozenset({"failed", "cancelled", "interrupted"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"queued", "rejected", "cancelled"}),
    "queued": frozenset({"preparing", "cancelled"}),
    "preparing": frozenset({"investigating", "awaiting_input"}) | _X,
    "investigating": frozenset(
        {"awaiting_plan_approval", "awaiting_command_approval", "awaiting_input"}
    )
    | _X,
    "awaiting_plan_approval": frozenset({"executing", "investigating"}) | _X,
    "executing": frozenset({"awaiting_command_approval", "awaiting_input", "verifying"}) | _X,
    "awaiting_command_approval": frozenset({"investigating", "executing", "verifying"}) | _X,
    "awaiting_input": frozenset({"preparing", "investigating", "executing", "verifying"}) | _X,
    "verifying": frozenset({"awaiting_command_approval", "awaiting_input", "confirming"}) | _X,
    "confirming": frozenset({"opening_pr", "completed"}) | _X,
    "opening_pr": frozenset({"completed"}) | _X,
    "completed": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
    "cancelled": frozenset(),
}


class InvalidTransition(Exception):
    pass


class TaskFinished(Exception):
    """The task row is already terminal (e.g. cancelled or interrupted concurrently)."""


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def check_transition(from_status: str, to_status: str) -> None:
    if from_status in TERMINAL_STATUSES:
        raise TaskFinished(f"task is already {from_status}")
    allowed = TRANSITIONS.get(from_status)
    if allowed is None or to_status not in allowed:
        raise InvalidTransition(f"{from_status} -> {to_status} is not allowed")
