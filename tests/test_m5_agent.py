"""`WebAgent` with the evidence harness: the completion gate, `record_evidence`, the read-only
backstop and its approval (m5-contract §4, §12 A1/A2). Real `WebAgent` + `TaskHarness`; the
model is scripted and the environment is a `FakeEnv` over a copy of seed 01."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import (
    FAILING_TEST,
    FIX_NEW,
    FIX_OLD,
    PLAN_ARGS,
    TEST_COMMAND,
    FakeChannel,
    FakeClock,
    ScriptedModel,
    Step,
    call,
    finish_steps,
    fix_steps,
    happy_steps,
    investigate_steps,
    observation,
    run_agent,
    run_tests_call,
)
from m5_fakes import (
    HarnessAgent,
    fix_and_finish_steps,
    new_env,
    pager_bash,
    write_command,
)

from swf import states
from swf.agent.channel import ApprovalOutcome
from swf.agent.web_agent import AcceptAllEvidence, EvidenceHarness, RefuseAllEvidence, WebAgent
from swf.evidence import REFUSAL_PREFIX

pytest_plugins = ["m4_fakes", "m5_fakes"]

MakeHarnessAgent = Callable[..., HarnessAgent]
Decide = Callable[[str, dict[str, Any]], ApprovalOutcome | None]


def suite_step(id: str = "suite1") -> Step:  # noqa: A002
    return [run_tests_call(None, id)]


def complete_step(id: str = "done1") -> Step:  # noqa: A002
    return [call("complete_task", id, summary="Fixed the offset.")]


def verified_steps() -> list[Step]:
    """Investigate, fix, target run (switches to verify), suite run: the bundle is complete."""
    return [*investigate_steps(), *fix_steps(), suite_step()]


# --- the completion gate --------------------------------------------------------------------


def test_complete_task_is_accepted_once_the_bundle_is_complete(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    agent, _, _, channel, harness, sink = make_harness_agent([*verified_steps(), complete_step()])
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert sink.kinds() == ["baseline", "baseline_suite", "post", "suite", "diff"]
    assert channel.events_of("completed") == [{"summary": "Fixed the offset."}]
    assert channel.events_of("evidence_check")[-1] == {"complete": True, "problems": []}
    assert harness.checked_digest is not None


def test_complete_task_is_refused_without_the_suite_run_and_names_the_code(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *investigate_steps(),
        *fix_steps(),
        complete_step("try1"),
        suite_step(),
        complete_step("try2"),
    ]
    agent, _, _, channel, _, sink = make_harness_agent(steps)
    result = run_agent(agent)
    refusal = observation(agent, "try1")
    assert REFUSAL_PREFIX in refusal
    assert "[suite_missing]" in refusal
    assert "[post_missing]" not in refusal
    assert {"complete": False, "problems": ["suite_missing"]} in channel.events_of("evidence_check")
    assert result["exit_status"] == "Submitted"
    assert sink.kinds()[-1] == "diff"


def test_a_bash_write_in_verify_makes_the_earlier_evidence_stale(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *verified_steps(),
        [call("bash", "late", command=write_command("late.txt"))],
        complete_step("try1"),
        [run_tests_call(FAILING_TEST, "again_t")],
        suite_step("again_s"),
        complete_step("try2"),
    ]
    agent, _, _, _, _, sink = make_harness_agent(steps)
    result = run_agent(agent)
    refusal = observation(agent, "try1")
    assert "[post_missing]" in refusal
    assert "[suite_missing]" in refusal
    assert result["exit_status"] == "Submitted"
    assert sink.kinds().count("post") == 2
    assert sink.kinds().count("suite") == 2


def test_every_covering_run_in_execute_is_recorded_not_only_the_passing_one(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    wip: Step = [call("edit_file", "wip", path="pager.py", old=FIX_OLD, new=FIX_OLD + "  # wip")]
    real: Step = [call("edit_file", "real", path="pager.py", old=FIX_OLD + "  # wip", new=FIX_NEW)]
    steps = [
        *investigate_steps(),
        wip,
        [run_tests_call(FAILING_TEST, "r1")],
        real,
        [run_tests_call(FAILING_TEST, "r2")],
        suite_step(),
        complete_step(),
    ]
    agent, _, _, _, _, sink = make_harness_agent(steps)
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert [r.exit_code for r in sink.of("post")] == [1, 0]
    assert all(r.source == "agent_run" and r.covers_target for r in sink.of("post"))


def test_evidence_is_taken_from_real_runs_not_from_what_the_agent_says(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *investigate_steps(),
        *fix_steps(),
        [call("record_evidence", "claim", kind="suite", note="all tests pass", command="pytest")],
        complete_step("try1"),
        suite_step(),
        complete_step("try2"),
    ]
    agent, _, _, _, _, sink = make_harness_agent(steps)
    run_agent(agent)
    assert "[suite_missing]" in observation(agent, "try1")
    assert sink.kinds() == ["baseline", "baseline_suite", "post", "suite", "diff"]


# --- record_evidence ------------------------------------------------------------------------


def test_record_evidence_returns_the_bundle_status_and_emits_a_note_without_a_row(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *investigate_steps(),
        *fix_steps(),
        [call("record_evidence", "ev1", kind="post", note="passes", command="pytest")],
        suite_step(),
        complete_step(),
    ]
    agent, _, _, channel, _, sink = make_harness_agent(steps)
    run_agent(agent)
    text = observation(agent, "ev1")
    assert "evidence bundle incomplete:" in text
    assert "[suite_missing]" in text
    assert channel.events_of("evidence_note") == [
        {"kind": "post", "note": "passes", "command": "pytest"}
    ]
    assert channel.events_of("evidence") == []  # the M4 stub event is gone with a harness
    assert sink.kinds() == ["baseline", "baseline_suite", "post", "suite", "diff"]


def test_record_evidence_reports_a_complete_bundle(make_harness_agent: MakeHarnessAgent) -> None:
    steps = [
        *verified_steps(),
        [call("record_evidence", "ev1", kind="suite", note="green")],
        complete_step(),
    ]
    agent, *_ = make_harness_agent(steps)
    run_agent(agent)
    assert "evidence bundle complete" in observation(agent, "ev1")


# --- gate defaults and opt-out --------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{}, {"evidence": None}, {"evidence": RefuseAllEvidence()}],
    ids=["omitted", "none", "refuse"],
)
def test_a_web_agent_without_a_gate_refuses_every_completion(
    tmp_path: Path, fake_clock: FakeClock, extra: dict[str, Any]
) -> None:
    steps = [*investigate_steps(), *fix_steps(), complete_step("try1")]
    channel = FakeChannel()
    agent = WebAgent(
        ScriptedModel(steps),
        new_env(tmp_path),
        channel=channel,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        test_command=TEST_COMMAND,
        allow=["python -m pytest*"],
        failing_test=FAILING_TEST,
        step_limit=len(steps),
        outputs_dir=tmp_path / "outputs",
        **extra,
    )
    result = run_agent(agent)
    assert "complete_task refused: no evidence harness is configured" in observation(agent, "try1")
    assert channel.events_of("completed") == []
    assert result["exit_status"] == "LimitsExceeded"


def test_accept_all_evidence_is_the_explicit_m4_opt_out(make_agent: Any, tmp_path: Path) -> None:
    agent, _, _, channel = make_agent(
        happy_steps(), bash=pager_bash(tmp_path / "repo"), evidence=AcceptAllEvidence()
    )
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert channel.events_of("backstop_violation") == []


def test_accept_all_evidence_has_no_backstop(make_agent: Any, tmp_path: Path) -> None:
    steps = [
        [call("read_file", "read1", path="pager.py")],
        [call("bash", "w1", command=write_command("notes.txt"))],
        [call("submit_plan", "plan1", **PLAN_ARGS)],
        *fix_steps(),
        *finish_steps(),
    ]
    agent, _, _, channel = make_agent(
        steps, bash=pager_bash(tmp_path / "repo"), evidence=AcceptAllEvidence()
    )
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert [kind for kind, _ in channel.approvals.values()] == ["command", "plan"]


def test_the_harness_is_an_evidence_harness_and_the_opt_outs_are_not(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    _, _, _, _, harness, _ = make_harness_agent([])
    assert isinstance(harness, EvidenceHarness)
    assert not isinstance(AcceptAllEvidence(), EvidenceHarness)
    assert not isinstance(RefuseAllEvidence(), EvidenceHarness)


# --- the read-only backstop (A1) ------------------------------------------------------------


def write_steps(
    name: str = "notes.txt", text: str = "x", *, bash_id: str = "w1", plan_id: str = "plan1"
) -> list[Step]:
    return [
        [call("read_file", "read1", path="pager.py")],
        [call("bash", bash_id, command=write_command(name, text))],
        [call("submit_plan", plan_id, **PLAN_ARGS)],
    ]


def deciding(readonly: str | None, plans: tuple[str, ...] = ("approved",)) -> Decide:
    """Approves commands; answers `readonly_violation` with `readonly` (None = never decided)
    and successive plan approvals with `plans`."""
    pending = list(plans)

    def decide(kind: str, payload: dict[str, Any]) -> ApprovalOutcome | None:
        if kind == "readonly_violation":
            return None if readonly is None else ApprovalOutcome(readonly, "no")
        if kind == "plan":
            return ApprovalOutcome(pending.pop(0), None)
        return ApprovalOutcome("approved", None)

    return decide


def kinds_requested(channel: FakeChannel) -> list[str]:
    return [kind for kind, _ in channel.approvals.values()]


def test_a_write_in_investigate_asks_for_approval_before_the_plan(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = FakeChannel(decide=deciding("rejected"))
    agent, _, _, channel, _, _ = make_harness_agent(write_steps(), channel=channel)
    run_agent(agent)
    (violation,) = channel.events_of("backstop_violation")
    assert violation["added"] == ["notes.txt"]
    assert violation["counts"] == {"added": 1, "removed": 0, "modified": 0}
    assert kinds_requested(channel) == ["command", "readonly_violation"]  # no plan approval
    _, payload = list(channel.approvals.values())[1]
    assert payload["added"] == ["notes.txt"]
    assert payload["counts"] == {"added": 1, "removed": 0, "modified": 0}
    assert "notes.txt" in payload["diff"]
    assert payload["diff_truncated"] is False
    assert channel.statuses() == [
        "awaiting_command_approval",  # the python -c command
        "investigating",
        "awaiting_command_approval",  # the readonly_violation approval
        "investigating",
    ]


def test_rejecting_the_change_ends_the_task_with_read_only_violation(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = FakeChannel(decide=deciding("rejected"))
    agent, _, _, channel, _, sink = make_harness_agent(write_steps(), channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "ReadOnlyViolation"
    assert "plan" not in kinds_requested(channel)
    assert sink.kinds() == ["baseline", "baseline_suite"]  # no re-baseline


def test_the_approval_payload_carries_the_head_of_the_diff(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = FakeChannel(decide=deciding("rejected"))
    agent, _, _, channel, _, _ = make_harness_agent(write_steps(text="y" * 5000), channel=channel)
    run_agent(agent)
    _, payload = list(channel.approvals.values())[1]
    assert len(payload["diff"]) <= 4000
    assert payload["diff_truncated"] is True
    assert "--- /dev/null" in payload["diff"]


def test_approving_the_change_rebaselines_and_excludes_it_from_the_final_diff(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [*write_steps(), *fix_and_finish_steps()]
    channel = FakeChannel(decide=deciding("approved"))
    agent, _, _, channel, harness, sink = make_harness_agent(steps, channel=channel)
    start_before = harness.start_digest
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert kinds_requested(channel) == ["command", "readonly_violation", "plan"]
    assert sink.kinds() == [
        "baseline",
        "baseline_suite",
        "baseline",
        "baseline_suite",
        "post",
        "suite",
        "diff",
    ]
    assert [bool(r.details.get("rebaseline")) for r in sink.records[:4]] == [
        False,
        False,
        True,
        True,
    ]
    assert harness.start_digest != start_before
    (rebase,) = channel.events_of("backstop_rebaseline")
    assert rebase["approval_id"] == str(list(channel.approvals)[1])
    diff = sink.of("diff")[0].output
    assert diff is not None
    assert "pager.py" in diff
    assert "notes.txt" not in diff
    assert channel.statuses()[:5] == [
        "awaiting_command_approval",
        "investigating",
        "awaiting_command_approval",
        "investigating",
        "awaiting_plan_approval",
    ]


def test_completion_after_an_accepted_change_still_needs_the_agents_own_evidence(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *write_steps(),
        *fix_steps(),
        complete_step("try1"),
        suite_step(),
        complete_step("try2"),
    ]
    channel = FakeChannel(decide=deciding("approved"))
    agent, _, _, channel, _, sink = make_harness_agent(steps, channel=channel)
    result = run_agent(agent)
    assert "[suite_missing]" in observation(agent, "try1")
    assert result["exit_status"] == "Submitted"
    assert sink.of("diff")[0].output is not None
    assert "notes.txt" not in str(sink.of("diff")[0].output)


def test_an_accepted_change_that_fixes_the_target_is_not_a_fix_by_the_agent(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = write_steps("pager.py", "FIXED")
    channel = FakeChannel(decide=deciding("approved"))
    agent, _, _, channel, _, _ = make_harness_agent(steps, channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "BaselineNotReproduced"
    assert kinds_requested(channel) == ["command", "readonly_violation"]


def test_an_unanswered_approval_expires_after_24_hours(
    make_harness_agent: MakeHarnessAgent, fake_clock: FakeClock
) -> None:
    channel = FakeChannel(decide=deciding(None))
    agent, _, _, channel, _, _ = make_harness_agent(write_steps(), channel=channel)
    started = fake_clock.now
    result = run_agent(agent)
    assert result["exit_status"] == "ApprovalTimeout"
    readonly_id = list(channel.approvals)[1]
    assert channel.expired_approvals == [readonly_id]
    assert fake_clock.now - started >= 86400
    assert "plan" not in kinds_requested(channel)


class OrderedChannel(FakeChannel):
    """Records the status each approval was requested under; can end the task mid-transition."""

    def __init__(self, *, finish_on_awaiting: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.status_at_request: list[tuple[str, str]] = []
        self._awaiting = 0
        self._finish_on = finish_on_awaiting

    def transition(self, to_status: str, **kwargs: Any) -> None:
        if to_status == "awaiting_command_approval":
            self._awaiting += 1
            if self._awaiting == self._finish_on:
                raise states.TaskFinished("task finished")
        super().transition(to_status, **kwargs)

    def request_approval(self, kind: str, payload: dict[str, Any]) -> Any:
        self.status_at_request.append((kind, self.status()))
        return super().request_approval(kind, payload)


def test_the_awaiting_status_is_set_before_the_readonly_approval_row_exists(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = OrderedChannel(decide=deciding("rejected"))
    agent, *_ = make_harness_agent(write_steps(), channel=channel)
    run_agent(agent)
    assert channel.status_at_request == [
        ("command", "awaiting_command_approval"),
        ("readonly_violation", "awaiting_command_approval"),
    ]


def test_a_finished_task_gets_no_readonly_approval_row(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    # 1st awaiting transition = the python -c command; the 2nd = the readonly_violation one
    channel = OrderedChannel(finish_on_awaiting=2, decide=deciding("approved"))
    agent, *_ = make_harness_agent(write_steps(), channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "Stopped"
    assert kinds_requested(channel) == ["command"]
    assert channel.events_of("backstop_rebaseline") == []


def test_a_clean_investigate_asks_only_for_the_plan(make_harness_agent: MakeHarnessAgent) -> None:
    steps = [*investigate_steps(), *fix_and_finish_steps()]
    agent, _, _, channel, _, _ = make_harness_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert kinds_requested(channel) == ["plan"]
    assert channel.events_of("backstop_violation") == []
    assert channel.events_of("backstop_rebaseline") == []


def test_a_second_violation_after_rebaselining_asks_again_against_the_new_start(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    steps = [
        *write_steps("a.txt", bash_id="w1", plan_id="plan1"),
        [call("bash", "w2", command=write_command("b.txt"))],
        [call("submit_plan", "plan2", **PLAN_ARGS)],
        *fix_and_finish_steps(),
    ]
    channel = FakeChannel(decide=deciding("approved", plans=("rejected", "approved")))
    agent, _, _, channel, _, sink = make_harness_agent(steps, channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert kinds_requested(channel) == [
        "command",
        "readonly_violation",
        "plan",
        "command",
        "readonly_violation",
        "plan",
    ]
    violations = channel.events_of("backstop_violation")
    assert [v["added"] for v in violations] == [["a.txt"], ["b.txt"]]
    assert len(channel.events_of("backstop_rebaseline")) == 2
    diff = sink.of("diff")[0].output
    assert diff is not None and "a.txt" not in diff and "b.txt" not in diff


def test_a_modified_file_in_investigate_is_reported_as_modified(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = FakeChannel(decide=deciding("rejected"))
    agent, _, _, channel, _, _ = make_harness_agent(
        write_steps("pytest.ini", "[pytest]"), channel=channel
    )
    run_agent(agent)
    (violation,) = channel.events_of("backstop_violation")
    assert violation["modified"] == ["pytest.ini"]
    assert violation["added"] == []


def test_the_model_is_not_queried_again_after_a_rejected_change(
    make_harness_agent: MakeHarnessAgent,
) -> None:
    channel = FakeChannel(decide=deciding("rejected"))
    agent, model, _, _, _, _ = make_harness_agent(
        [*write_steps(), *fix_and_finish_steps()], channel=channel
    )
    run_agent(agent)
    assert model.queries == 3  # read, write, submit_plan
    assert model.steps  # the fix steps were never asked for
