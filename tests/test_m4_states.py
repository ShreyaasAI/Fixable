"""Task state machine (m4-contract §2): the table, terminals, and `check_transition`."""

from __future__ import annotations

import pytest

from swf import states, store

FAIL = {"failed", "cancelled", "interrupted"}
TERMINAL = {"completed", "rejected", "failed", "interrupted", "cancelled"}

EXPECTED: dict[str, set[str]] = {
    "created": {"queued", "rejected", "cancelled"},
    "queued": {"preparing", "cancelled"},
    "preparing": {"investigating", "awaiting_input"} | FAIL,
    "investigating": {
        "awaiting_plan_approval",
        "awaiting_command_approval",
        "awaiting_input",
    }
    | FAIL,
    "awaiting_plan_approval": {"executing", "investigating"} | FAIL,
    "executing": {"awaiting_command_approval", "awaiting_input", "verifying"} | FAIL,
    "awaiting_command_approval": {"investigating", "executing", "verifying"} | FAIL,
    "awaiting_input": {"preparing", "investigating", "executing", "verifying"} | FAIL,
    "verifying": {"awaiting_command_approval", "awaiting_input", "confirming"} | FAIL,
    "confirming": {"opening_pr", "completed"} | FAIL,
    "opening_pr": {"completed"} | FAIL,
    "completed": set(),
    "rejected": set(),
    "failed": set(),
    "interrupted": set(),
    "cancelled": set(),
}


def test_transition_table_is_exactly_the_contract_table() -> None:
    actual = {k: set(v) for k, v in states.TRANSITIONS.items()}
    assert actual == EXPECTED


def test_every_store_status_is_a_row_in_the_table() -> None:
    assert set(states.TRANSITIONS) == set(store.TASK_STATUSES)


def test_terminal_statuses_have_no_exits() -> None:
    for status in TERMINAL:
        assert states.TRANSITIONS[status] == frozenset()
    assert set(store.TERMINAL_STATUSES) == TERMINAL


def test_table_values_are_frozensets() -> None:
    assert all(isinstance(v, frozenset) for v in states.TRANSITIONS.values())


def test_phase_status_mapping() -> None:
    assert states.PHASE_STATUS == {
        "prepare": "preparing",
        "investigate": "investigating",
        "plan": "awaiting_plan_approval",
        "execute": "executing",
        "verify": "verifying",
        "confirm": "confirming",
        "pr": "opening_pr",
    }


def test_waiting_statuses() -> None:
    assert (
        frozenset({"awaiting_plan_approval", "awaiting_command_approval", "awaiting_input"})
        == states.WAITING_STATUSES
    )


def test_a_wait_can_return_to_the_status_of_the_phase_it_started_in() -> None:
    # command approval and input waits start in investigate/execute/verify (and input in prepare)
    for phase in ("investigate", "execute", "verify"):
        back = states.PHASE_STATUS[phase]
        for wait in ("awaiting_command_approval", "awaiting_input"):
            states.check_transition(wait, back)
    states.check_transition("awaiting_input", states.PHASE_STATUS["prepare"])


@pytest.mark.parametrize(
    ("src", "dst"), [(s, d) for s, targets in EXPECTED.items() for d in sorted(targets)]
)
def test_allowed_transitions_pass(src: str, dst: str) -> None:
    states.check_transition(src, dst)  # does not raise


def test_invalid_transition_raises() -> None:
    with pytest.raises(states.InvalidTransition):
        states.check_transition("queued", "executing")
    with pytest.raises(states.InvalidTransition):
        states.check_transition("executing", "awaiting_plan_approval")
    with pytest.raises(states.InvalidTransition):
        states.check_transition("investigating", "investigating")


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
def test_a_terminal_row_raises_task_finished(terminal: str) -> None:
    with pytest.raises(states.TaskFinished):
        states.check_transition(terminal, "investigating")
    with pytest.raises(states.TaskFinished):
        states.check_transition(terminal, terminal)


def test_exception_types_are_distinct_exceptions() -> None:
    assert issubclass(states.InvalidTransition, Exception)
    assert issubclass(states.TaskFinished, Exception)
