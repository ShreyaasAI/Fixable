"""Task event types (m10a-contract §1.1).

Every event the worker, agent, harness or store appends has one of these types; `check_type`
rejects anything else so a typo cannot reach the UI. `NOISE_TYPES` are hidden behind the UI's
noise toggle (counted, not dropped). `web/src/events.ts` mirrors both sets; a test checks it.
Payloads stay plain dicts.
"""

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "status",
        "message",
        "tool_call",
        "tool_result",
        "tool_refused",
        "retry",
        "setup",
        "repo_cloned",
        "phase_model",
        "phase_switch",
        "completed",
        "guard_check",
        "guard_flag",
        "output_guard",
        "command_verdict",
        "approval_requested",
        "approval_decided",
        "approval_expired",
        "question_asked",
        "question_answered",
        "question_expired",
        "evidence",
        "evidence_note",
        "evidence_check",
        "evidence_recorded",
        "baseline_not_reproduced",
        "backstop_snapshot",
        "backstop_rebaseline",
        "backstop_violation",
        "harness_discrepancy",
        "harness_rerun",
        "pr_opened",
    }
)

NOISE_TYPES: frozenset[str] = frozenset(
    {
        "guard_check",
        "phase_model",
        "backstop_snapshot",
        "backstop_rebaseline",
        "command_verdict",
        "output_guard",
        "evidence_check",
        "retry",
    }
)


def check_type(type: str) -> str:
    """Return `type` if it is a known event type, else raise `ValueError`."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type!r}")
    return type
