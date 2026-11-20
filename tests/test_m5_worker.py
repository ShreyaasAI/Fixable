"""`TaskRunner` with the default evidence harness (m5-contract §6, §12 A1/A2): the evidence rows
a task leaves behind, the confirming step, and the new failure reasons. Private database clone,
scripted model, `FakeEnv` instead of Docker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from m4_fakes import (
    FAILING_TEST,
    PLAN_ARGS,
    AutoDecider,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    finish_steps,
    fix_steps,
    happy_steps,
    investigate_steps,
    queued_task,
    run_tests_call,
    status_events,
    task_row,
)
from m5_fakes import (
    ApproveOnly,
    approvals_of,
    events_of_type,
    evidence_rows,
    fake_pytest_output,
    fix_and_finish_steps,
    is_harness,
    is_harness_suite,
    is_harness_target,
    is_suite,
    is_target,
    pager_bash,
    scripted_bash,
    write_command,
    write_source,
)

from swf import store
from swf.agent.web_agent import AcceptAllEvidence
from swf.db import Database
from swf.worker import (
    EnvRequest,
    LocalCopyWorkspace,
    RunnerDeps,
    TaskRunner,
    WorkerConfig,
)

pytest_plugins = ["m4_fakes", "m5_fakes"]

BashFactory = Callable[[Path], Any]


@dataclass
class Rig:
    deps: RunnerDeps
    workspaces: Path
    envs: list[FakeEnv] = field(default_factory=list)
    models: list[ScriptedModel] = field(default_factory=list)


def whole_flow() -> list[Step]:
    """Investigate, fix, target run, suite run, complete."""
    return [*investigate_steps(), *fix_steps(), [run_tests_call(None, "suite1")], *finish_steps()]


def make_rig(
    db: Database,
    tmp_path: Path,
    *,
    steps: Callable[[], list[Step]] = whole_flow,
    bash: BashFactory = pager_bash,
    evidence: Any = None,
    agents_md: bool = True,
    fail_first: int = 0,
    agent: dict[str, Any] | None = None,
) -> Rig:
    workspaces = tmp_path / "workspaces"
    config = WorkerConfig(
        worker_id="w1",
        workspaces_root=workspaces,
        poll_seconds=0.05,
        agent=agent if agent is not None else {"poll_seconds": 0.02, "retry_backoff_seconds": []},
    )
    rig = Rig(deps=None, workspaces=workspaces)  # type: ignore[arg-type]

    def env_factory(request: EnvRequest) -> FakeEnv:
        env = FakeEnv(request.workspace, bash(request.workspace))
        if fail_first:
            env.fail_next(fail_first)
        rig.envs.append(env)
        return env

    def model_factory() -> ScriptedModel:
        model = ScriptedModel(steps())
        rig.models.append(model)
        return model

    rig.deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(write_source(tmp_path, agents_md=agents_md), workspaces),
        env_factory=env_factory,
        model_factory=model_factory,
        config=config,
        evidence=evidence,
    )
    return rig


def run_one(
    rig: Rig, db: Database, *, decider: Any = None, **task_kwargs: Any
) -> tuple[store.Task, str]:
    task = queued_task(db, **task_kwargs)
    with db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=90)
    assert claimed is not None and claimed.id == task.id
    with decider if decider is not None else AutoDecider(db):
        status = TaskRunner(rig.deps, claimed).run()
    return task, status


def kinds(db: Database, task: store.Task) -> list[str]:
    return [r.kind for r in evidence_rows(db, task.id)]


def error_of(db: Database, task: store.Task) -> str:
    return str(task_row(db, task.id)["error"])


def status_path(db: Database, task: store.Task) -> list[str]:
    return [e["to"] for e in status_events(db, task.id)]


# --- the default harness --------------------------------------------------------------------


def test_a_task_with_the_default_harness_completes_and_leaves_the_whole_bundle(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    task, status = run_one(rig, fresh_db)
    assert status == "completed"
    rows = evidence_rows(fresh_db, task.id)
    assert [r.kind for r in rows] == [
        "baseline",
        "baseline_suite",
        "post",
        "suite",
        "diff",
        "harness_rerun",
    ]
    assert [r.source for r in rows] == ["harness", "harness", "agent_run", "agent_run"] + [
        "harness",
        "harness",
    ]
    assert [r.exit_code for r in rows if r.kind != "diff"] == [1, 1, 0, 0, 0]
    assert status_path(fresh_db, task)[-3:] == ["verifying", "confirming", "completed"]
    assert len(events_of_type(fresh_db, task.id, "evidence_recorded")) == 6
    diff = next(r for r in rows if r.kind == "diff")
    assert diff.output is not None and "--- a/pager.py" in diff.output


def test_evidence_files_and_the_baseline_copy_live_in_the_task_workspace(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    task, _ = run_one(rig, fresh_db)
    root = rig.workspaces / str(task.id)
    assert (root / "baseline" / "pager.py").is_file()
    for row in evidence_rows(fresh_db, task.id):
        assert row.output_ref is not None
        assert not Path(row.output_ref).is_absolute()
        assert (root / row.output_ref).is_file()


def test_the_harness_runs_the_baseline_before_the_agent_starts(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    task, _ = run_one(rig, fresh_db)
    (env,) = rig.envs
    assert is_harness_target(env.executed[0])
    assert is_harness_suite(env.executed[1])
    assert status_path(fresh_db, task)[:3] == ["queued", "preparing", "investigating"]
    assert rig.models[0].queries > 0


def test_opting_out_with_accept_all_evidence_writes_no_evidence(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=happy_steps, evidence=AcceptAllEvidence())
    task, status = run_one(rig, fresh_db)
    assert status == "completed"
    assert evidence_rows(fresh_db, task.id) == []
    (env,) = rig.envs
    assert not any(is_harness(c) for c in env.executed)
    assert env.executed == [f"python -m pytest -q {FAILING_TEST}"]


# --- baseline failures ----------------------------------------------------------------------


def passing_bash(root: Path) -> Any:
    return scripted_bash([("pytest", lambda n: (fake_pytest_output(), 0))])


def test_a_baseline_that_passes_fails_the_task_before_any_model_call(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, bash=passing_bash)
    task, status = run_one(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "baseline_not_reproduced"
    assert rig.models == [] or rig.models[0].queries == 0
    assert kinds(fresh_db, task)[0] == "baseline"
    assert rig.envs[0].cleanups == 1


def test_a_repo_without_a_test_command_fails_with_no_test_command(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, agents_md=False)
    task, status = run_one(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "no_test_command"
    assert rig.models == [] or rig.models[0].queries == 0
    assert evidence_rows(fresh_db, task.id) == []


def test_a_baseline_infra_failure_is_retried_then_asked_about(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, fail_first=3)
    task, status = run_one(rig, fresh_db, decider=AutoDecider(fresh_db, answer="retry"))
    assert status == "completed"
    retries = [e for e in events_of_type(fresh_db, task.id, "retry") if e["step"] == "baseline"]
    assert [e["attempt"] for e in retries] == [1, 2, 3]
    with fresh_db.backend() as conn:
        asked = conn.execute(
            "select source, answer from public.questions where task_id = %s", [task.id]
        ).fetchall()
    assert [(r["source"], r["answer"]) for r in asked] == [("system", "retry")]
    assert "awaiting_input" in status_path(fresh_db, task)
    assert kinds(fresh_db, task)[:2] == ["baseline", "baseline_suite"]  # only the successful run
    assert kinds(fresh_db, task).count("baseline") == 1


def test_giving_up_after_a_baseline_infra_failure_fails_the_task(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, fail_first=99)
    task, status = run_one(rig, fresh_db, decider=AutoDecider(fresh_db, answer="no"))
    assert status == "failed"
    assert error_of(fresh_db, task) == "retries_exhausted"
    assert evidence_rows(fresh_db, task.id) == []


# --- confirming -----------------------------------------------------------------------------


def mismatch_bash(root: Path) -> Any:
    """Baseline and re-run fail; the agent's own runs pass."""
    return scripted_bash(
        [
            (is_harness_target, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_target, lambda n: (fake_pytest_output(), 0)),
            (is_suite, lambda n: (fake_pytest_output(), 0)),
        ]
    )


def test_a_harness_rerun_that_disagrees_fails_the_task_and_keeps_the_evidence(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, bash=mismatch_bash)
    task, status = run_one(rig, fresh_db)
    assert status == "failed"
    error = error_of(fresh_db, task)
    assert error.startswith("harness_mismatch")
    assert "post-fix exit 0, harness re-run exit 1" in error
    assert kinds(fresh_db, task) == [
        "baseline",
        "baseline_suite",
        "post",
        "suite",
        "diff",
        "harness_rerun",
    ]
    assert evidence_rows(fresh_db, task.id)[-1].exit_code == 1
    (discrepancy,) = events_of_type(fresh_db, task.id, "harness_discrepancy")
    assert discrepancy["reason"] == "rerun_failed"
    assert status_path(fresh_db, task)[-2:] == ["confirming", "failed"]


def rerun_infra_bash(root: Path) -> Any:
    return scripted_bash(
        [
            (
                is_harness_target,
                lambda n: (fake_pytest_output(FAILING_TEST), 1) if n == 0 else ("", -1),
            ),
            (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_target, lambda n: (fake_pytest_output(), 0)),
            (is_suite, lambda n: (fake_pytest_output(), 0)),
        ]
    )


def test_a_rerun_that_never_runs_fails_with_harness_rerun_error(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, bash=rerun_infra_bash)
    task, status = run_one(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "harness_rerun_error"
    retries = [
        e for e in events_of_type(fresh_db, task.id, "retry") if e["step"] == "harness_rerun"
    ]
    assert [e["attempt"] for e in retries] == [1, 2, 3]
    assert "harness_rerun" not in kinds(fresh_db, task)
    with (
        fresh_db.backend() as conn
    ):  # no retry question: there is no confirming <-> awaiting_input edge
        asked = conn.execute(
            "select count(*) as n from public.questions where task_id = %s", [task.id]
        ).fetchone()
    assert asked is not None and asked["n"] == 0
    assert status_path(fresh_db, task)[-2:] == ["confirming", "failed"]


def test_a_rerun_that_recovers_after_an_infra_failure_completes(
    fresh_db: Database, tmp_path: Path
) -> None:
    def flaky(root: Path) -> Any:
        return scripted_bash(
            [
                (
                    is_harness_target,
                    lambda n: (
                        (fake_pytest_output(FAILING_TEST), 1)
                        if n == 0
                        else ("", -1)
                        if n == 1
                        else (fake_pytest_output(), 0)
                    ),
                ),
                (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
                (is_target, lambda n: (fake_pytest_output(), 0)),
                (is_suite, lambda n: (fake_pytest_output(), 0)),
            ]
        )

    rig = make_rig(fresh_db, tmp_path, bash=flaky)
    task, status = run_one(rig, fresh_db)
    assert status == "completed"
    assert kinds(fresh_db, task)[-1] == "harness_rerun"
    retries = [
        e for e in events_of_type(fresh_db, task.id, "retry") if e["step"] == "harness_rerun"
    ]
    assert [e["attempt"] for e in retries] == [1]


# --- the read-only backstop -----------------------------------------------------------------


def write_then_plan() -> list[Step]:
    return [
        [call("read_file", "read1", path="pager.py")],
        [call("bash", "w1", command=write_command("notes.txt"))],
        [call("submit_plan", "plan1", **PLAN_ARGS)],
    ]


def test_a_rejected_write_in_investigate_fails_the_task_and_keeps_partial_evidence(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=write_then_plan)
    task, status = run_one(
        rig, fresh_db, decider=AutoDecider(fresh_db, reject_kinds=["readonly_violation"])
    )
    assert status == "failed"
    assert error_of(fresh_db, task) == "readonly_violation"
    assert kinds(fresh_db, task) == ["baseline", "baseline_suite"]
    assert approvals_of(fresh_db, task.id) == [
        ("command", "approved"),
        ("readonly_violation", "rejected"),
    ]
    (violation,) = events_of_type(fresh_db, task.id, "backstop_violation")
    assert violation["added"] == ["notes.txt"]
    assert rig.envs[0].cleanups == 1


def test_an_approved_write_is_part_of_the_base_and_not_of_the_diff(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=lambda: [*write_then_plan(), *fix_and_finish_steps()])
    task, status = run_one(rig, fresh_db)
    assert status == "completed"
    rows = evidence_rows(fresh_db, task.id)
    assert [r.kind for r in rows] == [
        "baseline",
        "baseline_suite",
        "baseline",
        "baseline_suite",
        "post",
        "suite",
        "diff",
        "harness_rerun",
    ]
    assert [bool(r.details.get("rebaseline")) for r in rows[:4]] == [False, False, True, True]
    diff = next(r for r in rows if r.kind == "diff")
    assert diff.output is not None
    assert "pager.py" in diff.output
    assert "notes.txt" not in diff.output
    assert [k for k, _ in approvals_of(fresh_db, task.id)] == [
        "command",
        "readonly_violation",
        "plan",
    ]
    assert len(events_of_type(fresh_db, task.id, "backstop_rebaseline")) == 1


def test_an_unanswered_readonly_approval_times_out_and_fails_the_task(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(
        fresh_db,
        tmp_path,
        steps=write_then_plan,
        agent={"poll_seconds": 0.02, "retry_backoff_seconds": [], "approval_timeout_seconds": 2},
    )
    task, status = run_one(rig, fresh_db, decider=ApproveOnly(fresh_db, ["command"]))
    assert status == "failed"
    assert error_of(fresh_db, task) == "approval_timeout"
    assert approvals_of(fresh_db, task.id)[-1] == ("readonly_violation", "expired")
    assert kinds(fresh_db, task) == ["baseline", "baseline_suite"]
