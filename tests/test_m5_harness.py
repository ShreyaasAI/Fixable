"""`TaskHarness`: baseline, snapshots, backstop, evidence rows, the gate and the re-run
(m5-contract §2 "Behaviour", §12 A1). A `FakeEnv` stands in for the container; commands are
answered by `scripted_bash` (counters) or `pager_bash` (content-based)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from m4_fakes import FAILING_TEST, FIX_NEW, FIX_OLD, TEST_COMMAND, FakeChannel, FakeEnv
from m5_fakes import (
    OTHER_FAILURE,
    MemorySink,
    fake_pytest_output,
    harness_for,
    is_harness,
    is_harness_suite,
    is_harness_target,
    new_env,
    pager_bash,
    scripted_bash,
    write_command,
)

from swf.agent.channel import BaselineNotReproduced, NoTestCommand
from swf.evidence import (
    HARNESS_PREAMBLE,
    HARNESS_TEST_TIMEOUT_SECONDS,
    REFUSAL_PREFIX,
    HarnessInfraError,
    TaskHarness,
    take_snapshot,
)

OK = {"output": "", "returncode": 0, "exception_info": ""}


def passing() -> dict[str, Any]:
    return {"output": fake_pytest_output(), "returncode": 0, "exception_info": ""}


def failing(*ids: str) -> dict[str, Any]:
    return {
        "output": fake_pytest_output(*(ids or (FAILING_TEST,))),
        "returncode": 1,
        "exception_info": "",
    }


Rig = tuple[TaskHarness, MemorySink, FakeEnv, FakeChannel]


def rig(tmp_path: Path, bash: Any = None, **kwargs: Any) -> Rig:
    env = new_env(tmp_path, bash)
    channel = FakeChannel()
    harness, sink = harness_for(env, channel, tmp_path, **kwargs)
    return harness, sink, env, channel


def started(tmp_path: Path, bash: Any = None, **kwargs: Any) -> Rig:
    made = rig(tmp_path, bash, **kwargs)
    made[0].run_baseline()
    made[0].take_start_snapshot()
    return made


def fix(env: FakeEnv) -> None:
    path = env.root / "pager.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace(FIX_OLD, FIX_NEW), encoding="utf-8", newline="\n"
    )


def baseline_rules(*, target_exit: int = 1) -> Any:
    return [
        (
            is_harness_target,
            lambda n: (
                fake_pytest_output(FAILING_TEST) if target_exit else fake_pytest_output(),
                target_exit,
            ),
        ),
        (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST, OTHER_FAILURE), 1)),
    ]


# --- run_baseline ---------------------------------------------------------------------------


def test_run_baseline_runs_the_target_then_the_suite_and_writes_two_harness_rows(
    tmp_path: Path,
) -> None:
    harness, sink, env, _ = rig(tmp_path, scripted_bash(baseline_rules()))
    harness.run_baseline()
    assert sink.kinds() == ["baseline", "baseline_suite"]
    baseline, suite = sink.records
    assert (baseline.source, baseline.exit_code, baseline.selector) == ("harness", 1, FAILING_TEST)
    assert (suite.source, suite.exit_code, suite.selector) == ("harness", 1, None)
    assert baseline.failures == {FAILING_TEST}
    assert suite.failures == {FAILING_TEST, OTHER_FAILURE}
    assert baseline.duration_ms is not None
    assert len(env.executed) == 2
    assert FAILING_TEST in env.executed[0]
    assert FAILING_TEST not in env.executed[1]


def test_every_harness_command_carries_the_preamble_and_the_long_timeout(tmp_path: Path) -> None:
    harness, _, env, _ = rig(tmp_path, scripted_bash(baseline_rules()))
    harness.run_baseline()
    assert all(c.startswith(HARNESS_PREAMBLE) for c in env.executed)
    assert env.timeouts == [HARNESS_TEST_TIMEOUT_SECONDS] * 2


def test_baseline_output_is_stored_as_a_tail_with_the_full_text_in_a_file(tmp_path: Path) -> None:
    full = "A" * 500 + "THE-TAIL"
    bash = scripted_bash(
        [(is_harness_target, lambda n: (full, 1)), (is_harness_suite, lambda n: ("s", 1))]
    )
    harness, sink, _, _ = rig(tmp_path, bash, output_limit=100)
    harness.run_baseline()
    baseline = sink.records[0]
    assert baseline.output == full[-100:]
    assert baseline.output is not None and baseline.output.endswith("THE-TAIL")
    assert baseline.output_truncated is True
    assert baseline.output_ref is not None
    assert not Path(baseline.output_ref).is_absolute()
    assert (tmp_path / "ws" / baseline.output_ref).read_text(encoding="utf-8") == full
    suite = sink.records[1]
    assert suite.output == "s"
    assert suite.output_truncated is False
    assert suite.output_ref is not None
    assert (tmp_path / "ws" / suite.output_ref).read_text(encoding="utf-8") == "s"


def test_without_a_failing_test_the_baseline_is_one_shared_suite_run(tmp_path: Path) -> None:
    harness, sink, env, _ = rig(
        tmp_path, scripted_bash([(is_harness, lambda n: (fake_pytest_output(OTHER_FAILURE), 1))]),
        failing_test=None,
    )  # fmt: skip
    harness.run_baseline()
    assert sink.kinds() == ["baseline", "baseline_suite"]
    assert len(env.executed) == 1
    assert all(r.details.get("shared_run") is True for r in sink.records)
    assert all(r.selector is None and r.exit_code == 1 for r in sink.records)


def test_a_passing_target_baseline_is_not_reproduced(tmp_path: Path) -> None:
    harness, sink, _, channel = rig(tmp_path, scripted_bash(baseline_rules(target_exit=0)))
    with pytest.raises(BaselineNotReproduced):
        harness.run_baseline()
    assert channel.events_of("baseline_not_reproduced") == [{"exit_code": 0}]
    assert sink.records[0].kind == "baseline"
    assert sink.records[0].exit_code == 0


def test_no_test_command_is_refused_before_any_run(tmp_path: Path) -> None:
    harness, sink, env, _ = rig(tmp_path, test_command=None)
    with pytest.raises(NoTestCommand):
        harness.run_baseline()
    assert env.executed == []
    assert sink.records == []


def test_an_infra_failure_in_the_target_run_writes_nothing(tmp_path: Path) -> None:
    harness, sink, env, _ = rig(tmp_path, scripted_bash(baseline_rules()))
    env.fail_next(1)
    with pytest.raises(HarnessInfraError):
        harness.run_baseline()
    assert sink.records == []


def test_an_infra_failure_in_the_suite_run_writes_no_rows_at_all(tmp_path: Path) -> None:
    bash = scripted_bash(
        [
            (is_harness_target, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_harness_suite, lambda n: ("", -1)),
        ]
    )
    harness, sink, _, _ = rig(tmp_path, bash)
    with pytest.raises(HarnessInfraError):
        harness.run_baseline()
    assert sink.records == []


# --- take_start_snapshot and check_readonly -------------------------------------------------


def test_take_start_snapshot_records_s0_copies_the_workspace_and_emits_an_event(
    tmp_path: Path,
) -> None:
    harness, _, env, channel = rig(tmp_path, scripted_bash(baseline_rules()))
    assert harness.started is False
    snap = harness.take_start_snapshot()
    assert harness.started is True
    assert harness.start_digest == snap.digest == take_snapshot(env.root).digest
    assert (tmp_path / "ws" / "baseline" / "pager.py").read_text(encoding="utf-8") == (
        env.root / "pager.py"
    ).read_text(encoding="utf-8")
    assert channel.events_of("backstop_snapshot") == [{"digest": snap.digest, "files": snap.files}]


def test_check_readonly_before_the_start_snapshot_is_an_error(tmp_path: Path) -> None:
    harness, _, _, _ = rig(tmp_path)
    with pytest.raises(RuntimeError):
        harness.check_readonly()


def test_check_readonly_is_clean_for_an_untouched_workspace(tmp_path: Path) -> None:
    harness, _, env, _ = started(tmp_path, scripted_bash(baseline_rules()))
    (env.root / "__pycache__").mkdir()
    (env.root / "__pycache__" / "pager.cpython-312.pyc").write_bytes(b"junk")
    report = harness.check_readonly()
    assert report["added"] == report["removed"] == report["modified"] == []
    assert report["counts"] == {"added": 0, "removed": 0, "modified": 0}


def test_check_readonly_reports_added_removed_and_modified_paths(tmp_path: Path) -> None:
    harness, _, env, _ = started(tmp_path, scripted_bash(baseline_rules()))
    (env.root / "notes.txt").write_text("x", encoding="utf-8")
    (env.root / "pytest.ini").unlink()
    fix(env)
    report = harness.check_readonly()
    assert report["added"] == ["notes.txt"]
    assert report["removed"] == ["pytest.ini"]
    assert report["modified"] == ["pager.py"]
    assert report["counts"] == {"added": 1, "removed": 1, "modified": 1}


# --- rebaseline (A1) ------------------------------------------------------------------------


def test_rebaseline_runs_the_baseline_again_and_takes_a_new_start_snapshot(tmp_path: Path) -> None:
    harness, sink, env, channel = started(tmp_path, pager_bash(tmp_path / "repo"))
    old_digest = harness.start_digest
    (env.root / "notes.txt").write_text("accepted", encoding="utf-8")
    harness.rebaseline()
    assert sink.kinds() == ["baseline", "baseline_suite", "baseline", "baseline_suite"]
    assert all(r.details.get("rebaseline") is True for r in sink.records[2:])
    assert not any(r.details.get("rebaseline") for r in sink.records[:2])
    assert harness.start_digest != old_digest
    assert harness.start_digest == take_snapshot(env.root).digest
    assert (tmp_path / "ws" / "baseline" / "notes.txt").read_text(encoding="utf-8") == "accepted"
    (event,) = channel.events_of("backstop_rebaseline")
    assert event["digest"] == harness.start_digest
    assert event["files"] == take_snapshot(env.root).files
    assert harness.check_readonly()["counts"] == {"added": 0, "removed": 0, "modified": 0}


def test_rebaseline_whose_accepted_change_fixes_the_target_is_not_reproduced(
    tmp_path: Path,
) -> None:
    harness, _, env, _ = started(tmp_path, pager_bash(tmp_path / "repo"))
    fix(env)
    with pytest.raises(BaselineNotReproduced):
        harness.rebaseline()


def test_rebaseline_replaces_the_baseline_used_by_the_bundle(tmp_path: Path) -> None:
    harness, sink, env, _ = started(tmp_path, pager_bash(tmp_path / "repo"))
    harness.rebaseline()
    fix(env)
    harness.record_run(tool_call_id="t1", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="t2", selector=None, output=passing(), phase="verify")
    assert harness.check(FakeChannel()) is None
    diff = sink.of("diff")[0]
    assert diff.output is not None and "pager.py" in diff.output


def test_rebaseline_infra_failure_is_a_harness_infra_error(tmp_path: Path) -> None:
    harness, _, env, _ = started(tmp_path, pager_bash(tmp_path / "repo"))
    env.fail_next(1)
    with pytest.raises(HarnessInfraError):
        harness.rebaseline()


# --- record_run -----------------------------------------------------------------------------


def test_record_run_in_execute_for_the_target_is_a_post_row(tmp_path: Path) -> None:
    harness, sink, env, _ = started(tmp_path, scripted_bash(baseline_rules()))
    fix(env)
    harness.record_run(
        tool_call_id="call_7", selector=FAILING_TEST, output=passing(), phase="execute"
    )
    post = sink.records[-1]
    assert post.kind == "post"
    assert post.source == "agent_run"
    assert post.selector == FAILING_TEST
    assert post.exit_code == 0
    assert post.tool_call_id == "call_7"
    assert post.covers_target is True
    assert post.command == f"{TEST_COMMAND} {FAILING_TEST}"
    assert post.workspace_digest == take_snapshot(env.root).digest


def test_record_run_without_a_selector_is_a_suite_row_in_verify(tmp_path: Path) -> None:
    harness, sink, _, _ = started(tmp_path, scripted_bash(baseline_rules()))
    harness.record_run(
        tool_call_id="c", selector=None, output=failing(OTHER_FAILURE), phase="verify"
    )
    suite = sink.records[-1]
    assert (suite.kind, suite.source, suite.exit_code, suite.selector) == (
        "suite",
        "agent_run",
        1,
        None,
    )
    assert suite.failures == {OTHER_FAILURE}
    assert suite.covers_target is True
    assert suite.command == TEST_COMMAND


def test_record_run_stores_the_workspace_digest_taken_after_the_run(tmp_path: Path) -> None:
    harness, sink, env, _ = started(tmp_path, scripted_bash(baseline_rules()))
    harness.record_run(tool_call_id="a", selector=None, output=passing(), phase="execute")
    (env.root / "later.txt").write_text("x", encoding="utf-8")
    harness.record_run(tool_call_id="b", selector=None, output=passing(), phase="execute")
    first, second = sink.records[-2:]
    assert first.workspace_digest != second.workspace_digest
    assert second.workspace_digest == take_snapshot(env.root).digest


@pytest.mark.parametrize("phase", ["investigate", "plan", "confirm", "prepare"])
def test_record_run_ignores_other_phases(tmp_path: Path, phase: str) -> None:
    harness, sink, _, _ = started(tmp_path, scripted_bash(baseline_rules()))
    before = len(sink.records)
    harness.record_run(tool_call_id="c", selector=None, output=passing(), phase=phase)
    assert len(sink.records) == before


def test_record_run_ignores_infra_failures(tmp_path: Path) -> None:
    harness, sink, _, _ = started(tmp_path, scripted_bash(baseline_rules()))
    before = len(sink.records)
    harness.record_run(
        tool_call_id="c",
        selector=None,
        output={"output": "", "returncode": -1, "exception_info": "timed out"},
        phase="execute",
    )
    harness.record_run(
        tool_call_id="d",
        selector=None,
        output={"output": "", "returncode": 0, "exception_info": "docker exploded"},
        phase="execute",
    )
    assert len(sink.records) == before


def test_record_run_ignores_runs_that_do_not_cover_the_target(tmp_path: Path) -> None:
    harness, sink, _, _ = started(tmp_path, scripted_bash(baseline_rules()))
    before = len(sink.records)
    harness.record_run(
        tool_call_id="c",
        selector="tests/test_pager.py::test_first_page",
        output=passing(),
        phase="execute",
    )
    assert len(sink.records) == before


def test_record_run_with_no_failing_test_only_records_whole_suite_runs(tmp_path: Path) -> None:
    harness, sink, _, _ = started(
        tmp_path,
        scripted_bash([(is_harness, lambda n: (fake_pytest_output(OTHER_FAILURE), 1))]),
        failing_test=None,
    )
    before = len(sink.records)
    harness.record_run(tool_call_id="a", selector="tests/x.py", output=passing(), phase="execute")
    assert len(sink.records) == before
    harness.record_run(tool_call_id="b", selector=None, output=passing(), phase="execute")
    assert sink.records[-1].kind == "suite"


# --- check (the gate) and status_text -------------------------------------------------------


def test_check_refuses_an_incomplete_bundle_and_lists_every_problem(tmp_path: Path) -> None:
    harness, _, _, channel = started(tmp_path, scripted_bash(baseline_rules()))
    refusal = harness.check(channel)
    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)
    for code in ("post_missing", "suite_missing", "diff_empty"):
        assert f"\n- [{code}] " in refusal
    assert "baseline_missing" not in refusal
    assert channel.events_of("evidence_check") == [
        {"complete": False, "problems": ["post_missing", "suite_missing", "diff_empty"]}
    ]
    assert harness.checked_digest is None


def test_stale_evidence_is_refused_after_a_later_write(tmp_path: Path) -> None:
    harness, _, env, channel = started(tmp_path, pager_bash(tmp_path / "repo"))
    fix(env)
    harness.record_run(tool_call_id="t", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="s", selector=None, output=passing(), phase="verify")
    (env.root / "pager.py").write_text(
        (env.root / "pager.py").read_text(encoding="utf-8") + "# late\n",
        encoding="utf-8",
        newline="\n",
    )
    refusal = harness.check(channel)
    assert refusal is not None
    assert "[post_missing]" in refusal
    assert "[suite_missing]" in refusal


def test_a_complete_bundle_is_accepted_and_writes_the_diff_row(tmp_path: Path) -> None:
    harness, sink, env, channel = started(tmp_path, pager_bash(tmp_path / "repo"))
    fix(env)
    harness.record_run(tool_call_id="t", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="s", selector=None, output=passing(), phase="verify")
    assert harness.check(channel) is None
    diff = sink.records[-1]
    assert (diff.kind, diff.source) == ("diff", "harness")
    assert diff.output is not None
    lines = diff.output.splitlines()
    assert "--- a/pager.py" in lines
    assert "+++ b/pager.py" in lines
    assert "-    start = (page - 1) * size - 1 if page > 1 else 0" in lines
    assert "+    start = (page - 1) * size" in lines
    assert diff.details["files_changed"] == 1
    assert diff.output_ref is not None and diff.output_ref.endswith(".patch")
    assert (tmp_path / "ws" / diff.output_ref).read_text(encoding="utf-8") == diff.output
    assert channel.events_of("evidence_check")[-1] == {"complete": True, "problems": []}
    assert harness.checked_digest == take_snapshot(env.root).digest


def test_a_long_diff_keeps_the_head_in_the_row_and_the_whole_patch_in_the_file(
    tmp_path: Path,
) -> None:
    harness, sink, env, channel = started(tmp_path, pager_bash(tmp_path / "repo"), output_limit=200)
    fix(env)
    (env.root / "big.txt").write_text("line\n" * 500, encoding="utf-8", newline="\n")
    harness.record_run(tool_call_id="t", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="s", selector=None, output=passing(), phase="verify")
    assert harness.check(channel) is None
    diff = sink.records[-1]
    assert diff.output is not None and len(diff.output) <= 200
    assert diff.output_truncated is True
    assert diff.output_ref is not None
    patch = (tmp_path / "ws" / diff.output_ref).read_text(encoding="utf-8")
    assert patch.startswith(diff.output)
    assert len(patch) > 200


def test_status_text_reports_complete_or_the_missing_pieces(tmp_path: Path) -> None:
    harness, _, env, channel = started(tmp_path, pager_bash(tmp_path / "repo"))
    text = harness.status_text()
    assert text.startswith("evidence bundle incomplete:")
    assert "- [post_missing] " in text
    assert "- [suite_missing] " in text
    fix(env)
    harness.record_run(tool_call_id="t", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="s", selector=None, output=passing(), phase="verify")
    assert harness.status_text() == "evidence bundle complete"
    assert channel.events_of("evidence_check") == []  # status is a read, not a gate decision


# --- confirm (the harness re-run) -----------------------------------------------------------


def accepted(tmp_path: Path, bash: Any, **kwargs: Any) -> Rig:
    made = started(tmp_path, bash, **kwargs)
    harness, _, env, channel = made
    fix(env)
    harness.record_run(tool_call_id="t", selector=FAILING_TEST, output=passing(), phase="execute")
    harness.record_run(tool_call_id="s", selector=None, output=passing(), phase="verify")
    assert harness.check(channel) is None
    return made


def test_confirm_passes_when_the_rerun_passes_on_the_unchanged_workspace(tmp_path: Path) -> None:
    harness, sink, env, channel = accepted(tmp_path, pager_bash(tmp_path / "repo"))
    before = len(env.executed)
    result = harness.confirm()
    assert result.passed is True
    assert result.exit_code == 0
    assert result.discrepancy is None
    assert result.evidence_id == len(sink.records)
    rerun = sink.records[-1]
    assert (rerun.kind, rerun.source, rerun.exit_code) == ("harness_rerun", "harness", 0)
    assert rerun.details["passed"] is True
    assert rerun.details["expected_exit_code"] == 0
    assert isinstance(rerun.details["post_evidence_id"], int)
    post = sink.records[rerun.details["post_evidence_id"] - 1]  # the run the gate relied on
    assert (post.source, post.exit_code, post.covers_target) == ("agent_run", 0, True)
    assert len(env.executed) == before + 1
    assert env.executed[-1].startswith(HARNESS_PREAMBLE)
    assert FAILING_TEST in env.executed[-1]
    assert channel.events_of("harness_rerun") == [
        {"evidence_id": result.evidence_id, "exit_code": 0, "passed": True}
    ]
    assert channel.events_of("harness_discrepancy") == []


def rerun_fails_bash() -> Any:
    return scripted_bash(
        [
            (is_harness_target, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (lambda c: FAILING_TEST in c, lambda n: (fake_pytest_output(), 0)),
            (lambda c: "pytest" in c, lambda n: (fake_pytest_output(), 0)),
        ]
    )


def test_confirm_reports_a_rerun_that_fails_although_the_agent_run_passed(tmp_path: Path) -> None:
    harness, sink, _, channel = accepted(tmp_path, rerun_fails_bash())
    result = harness.confirm()
    assert result.passed is False
    assert result.exit_code == 1
    discrepancy = result.discrepancy
    assert discrepancy is not None
    assert discrepancy["reason"] == "rerun_failed"
    assert discrepancy["post_exit_code"] == 0
    assert discrepancy["rerun_exit_code"] == 1
    assert discrepancy["rerun_failures"] == [FAILING_TEST]
    assert 0 < len(discrepancy["output_tail"]) <= 2000
    assert isinstance(discrepancy["post_evidence_id"], int)
    rerun = sink.records[-1]
    assert (rerun.kind, rerun.exit_code, rerun.details["passed"]) == ("harness_rerun", 1, False)
    assert channel.events_of("harness_rerun")[-1]["passed"] is False
    assert channel.events_of("harness_discrepancy") == [discrepancy]


def test_confirm_does_not_rerun_when_the_workspace_changed_after_the_gate(tmp_path: Path) -> None:
    harness, sink, env, channel = accepted(tmp_path, pager_bash(tmp_path / "repo"))
    accepted_digest = harness.checked_digest
    (env.root / "sneaky.txt").write_text("changed after the gate", encoding="utf-8")
    runs, rows = len(env.executed), len(sink.records)
    result = harness.confirm()
    assert result.passed is False
    assert (result.exit_code, result.evidence_id) == (None, None)
    discrepancy = result.discrepancy
    assert discrepancy is not None
    assert discrepancy["reason"] == "workspace_changed"
    assert discrepancy["checked_digest"] == accepted_digest
    assert discrepancy["current_digest"] == take_snapshot(env.root).digest
    assert len(env.executed) == runs
    assert len(sink.records) == rows
    assert channel.events_of("harness_discrepancy") == [discrepancy]


def test_confirm_before_the_gate_has_accepted_anything_is_workspace_changed(
    tmp_path: Path,
) -> None:
    harness, _, _, _ = started(tmp_path, pager_bash(tmp_path / "repo"))
    result = harness.confirm()
    assert result.passed is False
    assert result.discrepancy is not None
    assert result.discrepancy["reason"] == "workspace_changed"


def test_confirm_infra_failure_raises_and_writes_no_row(tmp_path: Path) -> None:
    bash = scripted_bash(
        [
            (
                is_harness_target,
                lambda n: (fake_pytest_output(FAILING_TEST), 1) if n == 0 else ("", -1),
            ),
            (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (lambda c: "pytest" in c, lambda n: (fake_pytest_output(), 0)),
        ]
    )
    harness, sink, _, _ = accepted(tmp_path, bash)
    rows = len(sink.records)
    with pytest.raises(HarnessInfraError):
        harness.confirm()
    assert len(sink.records) == rows


def test_every_command_the_harness_runs_has_the_preamble(tmp_path: Path) -> None:
    harness, _, env, _ = accepted(tmp_path, pager_bash(tmp_path / "repo"))
    harness.confirm()
    assert len(env.executed) == 3  # baseline target, baseline suite, re-run
    assert all(c.startswith(HARNESS_PREAMBLE) for c in env.executed)
    assert HARNESS_PREAMBLE.startswith("export PYTHONDONTWRITEBYTECODE=1;")
    assert all(is_harness(c) for c in env.executed)


def test_the_agent_can_write_through_the_fake_runner(tmp_path: Path) -> None:
    """Guards the fake itself: the approved python -c writer touches the workspace."""
    env = new_env(tmp_path)
    assert env.execute({"command": write_command("w.txt", "hi")})["returncode"] == 0
    assert (env.root / "w.txt").read_text(encoding="utf-8") == "hi"
