"""`WebAgent` (m4-contract §5.4, amended by §12 A1/A3), driven through a scripted model, a fake
environment and a fake channel; nothing sleeps for real."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import (
    FAILING_TEST,
    PLAN_ARGS,
    FakeChannel,
    FakeClock,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    edit_step,
    finish_steps,
    fix_steps,
    happy_steps,
    investigate_steps,
    observation,
    run_agent,
    run_tests_call,
)
from minisweagent.exceptions import FormatError

from swf import policy
from swf.agent import prompts
from swf.agent.channel import ApprovalOutcome
from swf.agent.web_agent import WebAgent, WebAgentConfig

pytest_plugins = ["m4_fakes"]

MakeAgent = Callable[..., tuple[WebAgent, ScriptedModel, FakeEnv, FakeChannel]]

NOTE = (
    "\n[harness] Tests passed after your changes, so the task is now in the verify phase. "
    "edit_file and write_file are no longer available. Re-run the checks you need, record "
    "evidence, then call complete_task."
)
STRICT_PYTEST = {"output": "1 failed", "returncode": 1, "exception_info": ""}


def pager_text(env: FakeEnv) -> str:
    return (env.root / "pager.py").read_text(encoding="utf-8")


def tool_message_ids(agent: WebAgent) -> list[str]:
    return [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"]


def approving(kind_to_outcome: dict[str, ApprovalOutcome | None]) -> Callable[..., Any]:
    def decide(kind: str, payload: dict[str, Any]) -> ApprovalOutcome | None:
        return kind_to_outcome.get(kind, ApprovalOutcome("approved", None))

    return decide


# --- configuration -------------------------------------------------------------------------


def test_config_defaults_follow_the_contract() -> None:
    config = WebAgentConfig()
    assert config.step_limit == 150
    assert config.cost_limit == 0.0
    assert config.wall_time_limit_seconds == 3600
    assert config.approval_timeout_seconds == 86400
    assert config.poll_seconds == 2.0
    assert config.max_retries == 3
    assert tuple(config.retry_backoff_seconds) == (5.0, 15.0)
    assert config.output_limit == 16000
    assert config.test_command is None
    assert config.allow == []
    assert config.failing_test is None
    assert config.outputs_dir is None


# --- the happy path (A1/A3) -----------------------------------------------------------------


def test_happy_path_investigate_plan_execute_auto_verify_complete(make_agent: MakeAgent) -> None:
    agent, model, env, channel = make_agent(happy_steps())
    result = run_agent(agent)

    assert result["exit_status"] == "Submitted"
    assert channel.transitions == [
        ("awaiting_plan_approval", "plan"),
        ("executing", "execute"),
        ("verifying", "verify"),
    ]
    assert model.set_phase_calls == ["investigate", "execute", "verify"]
    assert model.phases == ["investigate", "investigate", "execute", "execute", "verify"]
    assert "- 1 if" not in pager_text(env)  # the edit really happened in the environment

    switch = channel.events_of("phase_switch")
    assert switch == [
        {
            "from": "execute",
            "to": "verify",
            "trigger": {
                "tool_call_id": "tests1",
                "selector": FAILING_TEST,
                "edits_since_approval": 1,
            },
        }
    ]
    models = channel.events_of("phase_model")
    assert [m["phase"] for m in models] == ["execute", "verify"]
    assert all(isinstance(m["model"], str) and m["model"] for m in models)
    kinds = [t for t, _ in channel.events]
    assert kinds[kinds.index("phase_switch") + 1] == "phase_model"
    assert channel.events_of("completed") == [{"summary": "Fixed the offset."}]


def test_the_triggering_observation_carries_the_verify_note(make_agent: MakeAgent) -> None:
    agent, *_ = make_agent(happy_steps())
    run_agent(agent)
    assert prompts.VERIFY_SWITCH_NOTE == NOTE
    assert NOTE in observation(agent, "tests1")


def test_verify_phase_changes_the_tools_the_agent_may_use(make_agent: MakeAgent) -> None:
    steps = [
        *investigate_steps(),
        *fix_steps(),
        [call("edit_file", "late-edit", path="pager.py", old="start", new="BAD")],
        [call("write_file", "late-write", path="evil.py", content="x")],
        *finish_steps(),
    ]
    agent, _, env, channel = make_agent(steps)
    run_agent(agent)
    for tool_call_id in ("late-edit", "late-write"):
        text = observation(agent, tool_call_id)
        assert "forbidden" in text
        assert "<returncode>126</returncode>" in text
    assert "BAD" not in pager_text(env)
    assert not (env.root / "evil.py").exists()
    assert len(channel.events_of("tool_refused")) == 2


def test_edit_and_write_are_refused_in_investigate(make_agent: MakeAgent) -> None:
    steps = [
        [call("edit_file", "e1", path="pager.py", old="start", new="BAD")],
        [call("write_file", "w1", path="evil.py", content="x")],
        *happy_steps(),
    ]
    agent, _, env, channel = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    for tool_call_id in ("e1", "w1"):
        text = observation(agent, tool_call_id)
        assert "forbidden" in text
        assert "<returncode>126</returncode>" in text
    assert "BAD" not in pager_text(env)
    assert not (env.root / "evil.py").exists()
    assert len(channel.events_of("tool_refused")) >= 2


def test_prompts_describe_the_flow_and_the_task(make_agent: MakeAgent) -> None:
    agent, *_ = make_agent(happy_steps())
    run_agent(agent)
    system, instance = agent.messages[0], agent.messages[1]
    assert system["role"] == "system"
    for word in ("submit_plan", "run_tests", "verify", "complete_task"):
        assert word in system["content"]
    for text in ("octo/widgets", FAILING_TEST, "Page 2 of paginate"):
        assert text in instance["content"]


# --- the harness-driven switch: no-switch cases (A3) ----------------------------------------


def _no_edit() -> tuple[list[Step], dict[str, Any]]:
    return [*investigate_steps(), [run_tests_call()]], {}


def _rc_nonzero() -> tuple[list[Step], dict[str, Any]]:
    return [*investigate_steps(), *fix_steps()], {"bash": lambda cmd: STRICT_PYTEST}


def _selector_misses_target() -> tuple[list[Step], dict[str, Any]]:
    return [*investigate_steps(), *fix_steps("tests/test_other.py")], {}


def _failed_edit() -> tuple[list[Step], dict[str, Any]]:
    bad_edit = [call("edit_file", "bad-edit", path="pager.py", old="not in the file", new="x")]
    return [*investigate_steps(), bad_edit, [run_tests_call()]], {}


def _read_only_bash() -> tuple[list[Step], dict[str, Any]]:
    return [*investigate_steps(), [call("bash", command="cat pager.py")], [run_tests_call()]], {}


def _failed_writer() -> tuple[list[Step], dict[str, Any]]:
    def bash(cmd: str) -> dict[str, Any]:
        return STRICT_PYTEST if cmd.startswith("touch") else {
            "output": "ok", "returncode": 0, "exception_info": "",
        }  # fmt: skip

    return [*investigate_steps(), [call("bash", command="touch x.txt")], [run_tests_call()]], {
        "bash": bash
    }


def _investigate_phase() -> tuple[list[Step], dict[str, Any]]:
    return [[run_tests_call()]], {}


@pytest.mark.parametrize(
    "scenario",
    [
        _no_edit,
        _rc_nonzero,
        _selector_misses_target,
        _failed_edit,
        _read_only_bash,
        _failed_writer,
        _investigate_phase,
    ],
    ids=lambda f: f.__name__.strip("_"),
)
def test_no_switch(make_agent: MakeAgent, scenario: Callable[[], tuple[list[Step], dict]]) -> None:
    steps, options = scenario()
    agent, model, _, channel = make_agent(steps, step_limit=len(steps), **options)
    result = run_agent(agent)
    assert result["exit_status"] == "LimitsExceeded"
    assert "verifying" not in channel.statuses()
    assert channel.events_of("phase_switch") == []
    assert "verify" not in model.set_phase_calls
    assert "verify" not in model.phases


def test_a_passing_run_without_a_switch_returns_the_plain_observation(
    make_agent: MakeAgent,
) -> None:
    steps, _ = _no_edit()
    agent, *_ = make_agent(steps, step_limit=len(steps))
    run_agent(agent)
    assert NOTE not in observation(agent, "tests1")
    assert "<returncode>0</returncode>" in observation(agent, "tests1")


def test_a_second_passing_run_in_verify_does_not_switch_again(make_agent: MakeAgent) -> None:
    steps = [*investigate_steps(), *fix_steps(), [run_tests_call(id="tests2")], *finish_steps()]
    agent, _, _, channel = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert len(channel.events_of("phase_switch")) == 1
    assert channel.statuses().count("verifying") == 1
    assert NOTE not in observation(agent, "tests2")


def test_the_edit_counter_resets_when_a_plan_is_approved_again(make_agent: MakeAgent) -> None:
    # White box on purpose (contract A1/A3 "Edge"): a second approval is only reachable by
    # putting the agent back into investigate, which the hook below does between two queries.
    plan = [call("submit_plan", "plan-again", **PLAN_ARGS)]
    steps: list[Step] = [
        [call("submit_plan", "plan-first", **PLAN_ARGS)],
        edit_step(),
        plan,
        [run_tests_call()],
    ]
    agent, model, _, channel = make_agent(steps, step_limit=len(steps))

    def back_to_investigate(query_number: int) -> None:
        if query_number == 3:
            agent.phase = "investigate"
            model.phase = "investigate"
            channel.force_status("investigating")

    model.before_query = back_to_investigate
    result = run_agent(agent)
    assert result["exit_status"] == "LimitsExceeded"
    assert channel.events_of("phase_switch") == []  # the edit before the 2nd approval is forgotten
    assert "verifying" not in channel.statuses()


def test_actions_after_the_triggering_run_are_not_executed(make_agent: MakeAgent) -> None:
    steps = [
        *investigate_steps(),
        edit_step(),
        [run_tests_call(), call("bash", "after-switch", command="echo after-switch")],
        *finish_steps(),
    ]
    agent, _, env, _ = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert "echo after-switch" not in env.executed
    assert "not executed" in observation(agent, "after-switch")


def test_actions_after_submit_plan_are_not_executed(make_agent: MakeAgent) -> None:
    steps: list[Step] = [
        [call("submit_plan", "plan1", **PLAN_ARGS), call("bash", "after-plan", command="echo hi")],
        edit_step(),
        [run_tests_call()],
        *finish_steps(),
    ]
    agent, _, env, _ = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert "echo hi" not in env.executed
    assert "not executed" in observation(agent, "after-plan")


# --- the harness-driven switch: switch cases (A3) -------------------------------------------


@pytest.mark.parametrize(
    ("failing_test", "selector"),
    [
        (FAILING_TEST, FAILING_TEST),
        (FAILING_TEST, FAILING_TEST + "[case-1]"),
        ("test_second_page", "tests/test_pager.py::test_second_page"),
        (None, "tests/anything.py"),
        (None, None),
        (FAILING_TEST, None),
    ],
    ids=["equal", "selector-contains-target", "target-is-substring", "no-target", "bare", "suite"],
)
def test_switch_when_the_run_covers_the_target(
    make_agent: MakeAgent, failing_test: str | None, selector: str | None
) -> None:
    steps = [*investigate_steps(), *fix_steps(selector), *finish_steps()]
    agent, _, _, channel = make_agent(steps, failing_test=failing_test)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    (switch,) = channel.events_of("phase_switch")
    assert switch["trigger"]["selector"] == selector
    assert switch["trigger"]["edits_since_approval"] == 1
    assert channel.statuses()[-1] == "verifying"


def test_a_bash_writer_that_succeeds_counts_as_an_edit(make_agent: MakeAgent) -> None:
    command = "sed -i 's/ - 1 if page > 1 else 0//' pager.py"
    assert policy.classify(command, phase="investigate", allow=["python -m pytest*"]).decision == (
        "forbid"
    )
    assert policy.classify(command, phase="execute", allow=["python -m pytest*"]).decision in {
        "allow",
        "approve",
    }
    steps = [*investigate_steps(), [call("bash", command=command)], [run_tests_call()]]
    agent, _, env, channel = make_agent(steps + finish_steps())
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert command in env.executed
    (switch,) = channel.events_of("phase_switch")
    assert switch["trigger"]["edits_since_approval"] == 1


# --- commands: approve / reject / forbid ----------------------------------------------------


def test_a_command_that_needs_approval_waits_and_runs_once_approved(make_agent: MakeAgent) -> None:
    command = "touch notes.txt"
    assert policy.classify(command, phase="execute").decision == "approve"
    steps = [
        *investigate_steps(),
        [call("bash", "touch1", command=command)],
        *fix_steps(),
        *finish_steps(),
    ]
    agent, _, env, channel = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert command in env.executed
    assert channel.statuses() == [
        "awaiting_plan_approval",
        "executing",
        "awaiting_command_approval",
        "executing",
        "verifying",
    ]
    command_approvals = [p for k, p in channel.approvals.values() if k == "command"]
    assert len(command_approvals) == 1
    assert command_approvals[0]["command"] == command
    assert command_approvals[0]["phase"] == "execute"
    assert command_approvals[0]["reasons"]
    verdicts = [v for v in channel.events_of("command_verdict") if v["command"] == command]
    assert verdicts[0]["decision"] == "approve"
    assert verdicts[0]["reasons"]
    (switch,) = channel.events_of("phase_switch")
    assert switch["trigger"]["edits_since_approval"] == 2  # the touch and the edit_file


def test_a_rejected_command_is_not_run_and_the_comment_reaches_the_model(
    make_agent: MakeAgent,
) -> None:
    command = "touch notes.txt"
    decide = approving({"command": ApprovalOutcome("rejected", "please do not create files")})
    steps = [
        *investigate_steps(),
        [call("bash", "touch1", command=command)],
        *fix_steps(),
        *finish_steps(),
    ]
    agent, _, env, channel = make_agent(steps, channel=FakeChannel(decide=decide))
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert command not in env.executed
    text = observation(agent, "touch1")
    assert "rejected by user: please do not create files" in text
    assert "<returncode>1</returncode>" in text
    assert channel.statuses()[2:4] == ["awaiting_command_approval", "executing"]
    (switch,) = channel.events_of("phase_switch")
    assert switch["trigger"]["edits_since_approval"] == 1  # the rejected command is no edit


def test_a_forbidden_command_never_runs_and_asks_nobody(make_agent: MakeAgent) -> None:
    steps = [[call("bash", "sudo1", command="sudo ls")], *happy_steps()]
    agent, _, env, channel = make_agent(steps)
    run_agent(agent)
    assert "sudo ls" not in env.executed
    text = observation(agent, "sudo1")
    assert "<returncode>126</returncode>" in text
    verdicts = [v for v in channel.events_of("command_verdict") if v["command"] == "sudo ls"]
    assert verdicts[0]["decision"] == "forbid"
    assert not any(k == "command" for k, _ in channel.approvals.values())


def test_a_safe_command_runs_without_asking(make_agent: MakeAgent) -> None:
    steps = [[call("bash", "ls1", command="ls")], *happy_steps()]
    agent, _, env, channel = make_agent(steps, bash={"ls": ("pager.py\ntests\n", 0)})
    run_agent(agent)
    assert "ls" in env.executed
    assert "pager.py" in observation(agent, "ls1")
    verdict = next(v for v in channel.events_of("command_verdict") if v["command"] == "ls")
    assert verdict["decision"] == "allow"
    assert not any(k == "command" for k, _ in channel.approvals.values())


def test_the_bash_timeout_argument_reaches_the_environment(make_agent: MakeAgent) -> None:
    steps = [[call("bash", "slow", command="ls", timeout_seconds=30)]]
    agent, _, env, _ = make_agent(steps, step_limit=1)
    run_agent(agent)
    assert 30 in env.timeouts


# --- plan approval --------------------------------------------------------------------------


def test_a_rejected_plan_returns_to_investigate_with_the_comment(make_agent: MakeAgent) -> None:
    verdicts = iter(
        [
            ApprovalOutcome("rejected", "also cover the first page"),
            ApprovalOutcome("approved", None),
        ]
    )
    channel = FakeChannel(decide=lambda kind, payload: next(verdicts))
    steps = [
        [call("submit_plan", "plan-1", **PLAN_ARGS)],
        [call("read_file", "read-again", path="tests/test_pager.py")],
        [call("submit_plan", "plan-2", **PLAN_ARGS)],
        *fix_steps(),
        *finish_steps(),
    ]
    agent, model, _, channel = make_agent(steps, channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert channel.transitions == [
        ("awaiting_plan_approval", "plan"),
        ("investigating", "investigate"),
        ("awaiting_plan_approval", "plan"),
        ("executing", "execute"),
        ("verifying", "verify"),
    ]
    assert "also cover the first page" in observation(agent, "plan-1")
    assert model.phases[:3] == ["investigate", "investigate", "investigate"]
    assert [m["phase"] for m in channel.events_of("phase_model")] == ["execute", "verify"]


def test_a_plan_request_carries_the_plan(make_agent: MakeAgent) -> None:
    agent, _, _, channel = make_agent(happy_steps())
    run_agent(agent)
    ((kind, payload),) = [v for v in channel.approvals.values() if v[0] == "plan"]
    assert kind == "plan"
    assert payload["summary"] == PLAN_ARGS["summary"]
    assert payload["steps"] == [PLAN_ARGS["steps"]]
    assert payload["files"] == PLAN_ARGS["files"]


def test_an_invalid_plan_is_an_observation_not_an_approval(make_agent: MakeAgent) -> None:
    bad = {**PLAN_ARGS, "steps": ""}
    steps = [[call("submit_plan", "bad-plan", **bad)], *happy_steps()]
    agent, _, _, channel = make_agent(steps)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    text = observation(agent, "bad-plan")
    assert "steps" in text
    assert "<returncode>1</returncode>" in text
    assert len([1 for k, _ in channel.approvals.values() if k == "plan"]) == 1
    assert channel.statuses()[0] == "awaiting_plan_approval"


# --- ask_user -------------------------------------------------------------------------------


def test_ask_user_round_trip(make_agent: MakeAgent, fake_clock: FakeClock) -> None:
    polls = {"n": 0}

    def answers(question: str) -> str | None:
        polls["n"] += 1
        return "use a page size of 10" if polls["n"] >= 3 else None

    steps = [[call("ask_user", "ask1", question="Which page size?")], *happy_steps()]
    agent, _, _, channel = make_agent(steps, channel=FakeChannel(answers=answers))
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert channel.asked == [("Which page size?", "agent")]
    assert "use a page size of 10" in observation(agent, "ask1")
    assert channel.statuses()[:2] == ["awaiting_input", "investigating"]
    assert fake_clock.sleeps[:2] == [2.0, 2.0]  # polled twice before the answer arrived


# --- timeouts -------------------------------------------------------------------------------


def test_the_default_approval_timeout_is_24_hours(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    channel = FakeChannel(decide=lambda kind, payload: None)
    agent, _, _, channel = make_agent(investigate_steps(), channel=channel)
    started = fake_clock.now
    result = run_agent(agent)
    assert result["exit_status"] == "ApprovalTimeout"
    assert 86400 <= fake_clock.now - started <= 86400 + 5
    assert list(channel.approvals) == channel.expired_approvals
    assert channel.statuses() == ["awaiting_plan_approval"]  # the runner, not the agent, fails it


def test_the_approval_timeout_is_configurable(make_agent: MakeAgent, fake_clock: FakeClock) -> None:
    channel = FakeChannel(decide=lambda kind, payload: None)
    agent, _, _, channel = make_agent(
        investigate_steps(), channel=channel, approval_timeout_seconds=300, poll_seconds=10
    )
    started = fake_clock.now
    result = run_agent(agent)
    assert result["exit_status"] == "ApprovalTimeout"
    assert 300 <= fake_clock.now - started <= 310
    assert len(channel.expired_approvals) == 1


def test_a_command_approval_can_time_out_too(make_agent: MakeAgent) -> None:
    decide = approving({"command": None})
    steps = [*investigate_steps(), [call("bash", command="touch notes.txt")]]
    agent, _, env, channel = make_agent(
        steps, channel=FakeChannel(decide=decide), approval_timeout_seconds=60
    )
    result = run_agent(agent)
    assert result["exit_status"] == "ApprovalTimeout"
    assert "touch notes.txt" not in env.executed
    assert len(channel.expired_approvals) == 1


def test_an_unanswered_question_times_out(make_agent: MakeAgent, fake_clock: FakeClock) -> None:
    steps = [[call("ask_user", question="Which page size?")]]
    agent, _, _, channel = make_agent(steps, approval_timeout_seconds=60)
    started = fake_clock.now
    result = run_agent(agent)
    assert result["exit_status"] == "QuestionTimeout"
    assert 60 <= fake_clock.now - started <= 70
    assert len(channel.expired_questions) == 1
    assert channel.expired_approvals == []


def test_waits_do_not_count_toward_the_wall_time_limit(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    started = fake_clock.now

    def decide(kind: str, payload: dict[str, Any]) -> ApprovalOutcome | None:
        return ApprovalOutcome("approved", None) if fake_clock.now - started >= 3600 else None

    agent, *_ = make_agent(
        happy_steps(), channel=FakeChannel(decide=decide), wall_time_limit_seconds=100
    )
    result = run_agent(agent)
    assert fake_clock.now - started >= 3600  # an hour was spent waiting for the human
    assert result["exit_status"] == "Submitted"


# --- cancellation ---------------------------------------------------------------------------


def test_cancel_between_steps_stops_the_agent(make_agent: MakeAgent) -> None:
    holder: list[FakeChannel] = []

    def bash(cmd: str) -> dict[str, Any]:
        holder[0].cancel()
        return {"output": "", "returncode": 0, "exception_info": ""}

    steps = [[call("bash", command="ls")], [call("bash", command="ls")]]
    agent, model, _, channel = make_agent(steps, bash=bash)
    holder.append(channel)
    result = run_agent(agent)
    assert result["exit_status"] == "Stopped"
    assert model.queries == 1


def test_cancel_during_a_wait_stops_the_agent_without_waiting_a_day(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    holder: list[FakeChannel] = []
    polls = {"n": 0}

    def decide(kind: str, payload: dict[str, Any]) -> ApprovalOutcome | None:
        polls["n"] += 1
        if polls["n"] == 3:
            holder[0].cancel()
        return None

    agent, _, _, channel = make_agent(investigate_steps(), channel=FakeChannel(decide=decide))
    holder.append(channel)
    started = fake_clock.now
    result = run_agent(agent)
    assert result["exit_status"] == "Stopped"
    assert fake_clock.now - started < 100
    assert channel.statuses() == ["awaiting_plan_approval"]


# --- model errors: retry, then a system question --------------------------------------------


def test_three_model_errors_ask_the_user_and_retry_resets_the_count(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    steps: list[Step] = [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
    steps += happy_steps()
    agent, model, _, channel = make_agent(steps, channel=FakeChannel(answers=lambda q: "retry"))
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert len(channel.asked) == 1
    assert channel.asked[0][1] == "system"
    assert fake_clock.sleeps == [5.0, 15.0]  # backoff between attempts, none before the question
    retries = [e for e in channel.events_of("retry") if e["step"] == "model"]
    assert len(retries) >= 2
    assert all("boom" in e["error"] for e in retries)
    assert "awaiting_input" in channel.statuses()
    assert model.queries == 3 + 5


@pytest.mark.parametrize("reply", ["retry", "  RETRY \n", "Retry"])
def test_retry_is_case_insensitive_and_trimmed(make_agent: MakeAgent, reply: str) -> None:
    steps: list[Step] = [RuntimeError("boom")] * 3 + happy_steps()
    agent, *_ = make_agent(steps, channel=FakeChannel(answers=lambda q: reply))
    assert run_agent(agent)["exit_status"] == "Submitted"


@pytest.mark.parametrize("reply", ["stop", "no thanks", "retry please", ""])
def test_any_other_answer_gives_up(make_agent: MakeAgent, reply: str) -> None:
    steps: list[Step] = [RuntimeError("boom")] * 3 + happy_steps()
    channel = FakeChannel(answers=lambda q: reply or " ")
    agent, model, _, channel = make_agent(steps, channel=channel)
    result = run_agent(agent)
    assert result["exit_status"] == "RetriesExhausted"
    assert model.queries == 3
    assert len(channel.asked) == 1


def test_retry_grants_three_more_attempts_and_then_asks_again(make_agent: MakeAgent) -> None:
    holder: list[FakeChannel] = []

    def answers(question: str) -> str:
        return "retry" if len(holder[0].asked) == 1 else "no"

    steps: list[Step] = [RuntimeError("boom")] * 6
    agent, model, _, channel = make_agent(steps, channel=FakeChannel(answers=answers))
    holder.append(channel)
    result = run_agent(agent)
    assert result["exit_status"] == "RetriesExhausted"
    assert model.queries == 6
    assert [source for _, source in channel.asked] == ["system", "system"]


def test_a_model_format_error_is_not_a_model_failure(make_agent: MakeAgent) -> None:
    bad = FormatError(
        {"role": "user", "content": "bad", "extra": {"interrupt_type": "FormatError"}}
    )
    agent, model, _, channel = make_agent([bad, bad, bad])
    result = run_agent(agent)
    assert result["exit_status"] == "RepeatedFormatError"
    assert model.queries == 3
    assert channel.events_of("retry") == []
    assert channel.asked == []


def test_model_cost_is_reported_to_the_channel(make_agent: MakeAgent) -> None:
    agent, _, _, channel = make_agent(happy_steps(), cost=0.02)
    run_agent(agent)
    assert sum(channel.costs) == pytest.approx(0.10)


def test_the_step_limit_stops_the_agent(make_agent: MakeAgent) -> None:
    steps: list[Step] = [[call("read_file", path="pager.py")] for _ in range(3)]
    agent, model, _, _ = make_agent(steps, step_limit=2)
    result = run_agent(agent)
    assert result["exit_status"] == "LimitsExceeded"
    assert model.queries == 2


# --- run_tests infrastructure failures ------------------------------------------------------


def test_an_infrastructure_failure_in_run_tests_is_retried(make_agent: MakeAgent) -> None:
    agent, _, env, channel = make_agent(happy_steps())
    env.fail_next(1, "infra")
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert [c for c in env.executed if "pytest" in c] == ["python -m pytest -q " + FAILING_TEST] * 2
    assert any(e["step"] == "run_tests" for e in channel.events_of("retry"))
    assert len(channel.events_of("phase_switch")) == 1  # only the successful run switched


def test_three_infrastructure_failures_ask_the_user_and_do_not_switch(
    make_agent: MakeAgent,
) -> None:
    agent, _, env, channel = make_agent(
        happy_steps(), channel=FakeChannel(answers=lambda q: "give up")
    )
    env.fail_next(3, "infra")
    result = run_agent(agent)
    assert result["exit_status"] == "RetriesExhausted"
    assert len([c for c in env.executed if "pytest" in c]) == 3
    assert channel.asked and channel.asked[0][1] == "system"
    assert "verifying" not in channel.statuses()
    assert channel.events_of("phase_switch") == []


def test_retry_after_infrastructure_failures_runs_the_tests_again(make_agent: MakeAgent) -> None:
    agent, _, env, channel = make_agent(
        happy_steps(), channel=FakeChannel(answers=lambda q: "retry")
    )
    env.fail_next(3, "infra")
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert len([c for c in env.executed if "pytest" in c]) == 4
    assert len(channel.events_of("phase_switch")) == 1


def test_a_failing_test_is_not_an_infrastructure_failure(make_agent: MakeAgent) -> None:
    steps = [*investigate_steps(), *fix_steps()]
    agent, _, env, channel = make_agent(
        steps, bash=lambda cmd: STRICT_PYTEST, step_limit=len(steps)
    )
    run_agent(agent)
    assert len([c for c in env.executed if "pytest" in c]) == 1  # no retry of a plain failure
    assert channel.events_of("retry") == []
    assert channel.asked == []


# --- tool errors and the verify tools -------------------------------------------------------


def test_a_tool_error_is_an_observation_not_a_crash(make_agent: MakeAgent) -> None:
    steps = [[call("read_file", "outside", path="../etc/passwd")], *happy_steps()]
    agent, *_ = make_agent(steps)
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert "<returncode>1</returncode>" in observation(agent, "outside")


def test_run_tests_without_a_test_command_is_reported_to_the_model(make_agent: MakeAgent) -> None:
    steps = [[run_tests_call(None, "rt")]]
    agent, _, env, _ = make_agent(steps, test_command=None, step_limit=1)
    run_agent(agent)
    text = observation(agent, "rt")
    assert "no test command configured in AGENTS.md" in text
    assert "<returncode>1</returncode>" in text
    assert env.executed == []


def test_record_evidence_is_a_recorded_stub(make_agent: MakeAgent) -> None:
    steps = [
        *investigate_steps(),
        *fix_steps(),
        [call("record_evidence", "ev1", kind="post", note="passes", command="pytest")],
        *finish_steps(),
    ]
    agent, *_ = make_agent(steps)
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert "recorded" in observation(agent, "ev1")


class RefuseOnce:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, channel: Any) -> str | None:
        self.calls += 1
        return "no baseline evidence has been recorded" if self.calls == 1 else None


def test_the_evidence_gate_can_refuse_completion(make_agent: MakeAgent) -> None:
    gate = RefuseOnce()
    steps = [
        *investigate_steps(),
        *fix_steps(),
        [call("complete_task", "try1", summary="done")],
        [call("complete_task", "try2", summary="done, with evidence")],
    ]
    agent, _, _, channel = make_agent(steps, evidence=gate)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert gate.calls == 2
    assert "no baseline evidence has been recorded" in observation(agent, "try1")
    assert channel.events_of("completed") == [{"summary": "done, with evidence"}]


def test_complete_task_with_the_default_gate_accepts_immediately(make_agent: MakeAgent) -> None:
    agent, *_ = make_agent(happy_steps())
    assert run_agent(agent)["exit_status"] == "Submitted"


# --- events ---------------------------------------------------------------------------------


def test_tool_events_and_message_events(make_agent: MakeAgent) -> None:
    steps = [[call("bash", "ls1", command="ls")], *happy_steps()]
    agent, _, _, channel = make_agent(steps)
    run_agent(agent)
    calls = {p["tool_call_id"]: p for p in channel.events_of("tool_call")}
    results = {p["tool_call_id"]: p for p in channel.events_of("tool_result")}
    assert calls["read1"]["tool"] == "read_file"
    assert calls["read1"]["args"] == {"path": "pager.py"}
    assert set(calls) == set(results)
    assert {"ls1", "read1", "plan1", "edit1", "tests1", "done1"} <= set(calls)
    for payload in results.values():
        assert {"tool", "tool_call_id", "returncode", "output", "truncated", "output_ref"} <= set(
            payload
        )
    assert results["read1"]["truncated"] is False
    assert "def paginate" in results["read1"]["output"]
    roles = [p["role"] for p in channel.events_of("message")]
    assert {"system", "user", "assistant", "tool", "exit"} <= set(roles)
    assert roles[0] == "system"
    exit_event = [p for p in channel.events_of("message") if p["role"] == "exit"][-1]
    assert exit_event["exit_status"] == "Submitted"
    assert any("model" in p for p in channel.events_of("message") if p["role"] == "assistant")
    verdict = next(v for v in channel.events_of("command_verdict") if v["command"] == "ls")
    assert verdict["decision"] == "allow"


def test_output_truncation_and_output_ref(make_agent: MakeAgent, tmp_path: Path) -> None:
    big = "line of output\n" * 4000
    steps = [[call("bash", "big1", command="ls")], [call("read_file", "small1", path="pager.py")]]
    agent, _, _, channel = make_agent(
        steps,
        bash=lambda cmd: {"output": big, "returncode": 0, "exception_info": ""},
        output_limit=1000,
        step_limit=2,
    )
    run_agent(agent)
    results = {p["tool_call_id"]: p for p in channel.events_of("tool_result")}
    large = results["big1"]
    assert len(large["output"]) <= 1000
    assert large["truncated"] is True
    assert isinstance(large["output_ref"], str)
    assert large["output_ref"].endswith("big1.txt")
    saved = tmp_path / "outputs" / "big1.txt"
    assert saved.is_file()
    assert big in saved.read_text(encoding="utf-8")
    small = results["small1"]
    assert small["truncated"] is False
