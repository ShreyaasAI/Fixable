"""M5 definition of done, for real: seeds through `Worker.run_once()` in a real sandbox with the
reference fix scripted as the model (m5-contract §7, §12 A2). Needs Docker and network (pip
install of pytest through the egress proxy). Docker objects are named `swf-test-<run>-…`."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import PLAN_ARGS, AutoDecider, ScriptedModel, Step, call, queued_task, task_row
from m5_fakes import (
    SEEDS,
    approvals_of,
    events_of_type,
    evidence_rows,
    reference_steps,
    seed_description,
    seed_reference_files,
    seed_target,
)

from swf import store
from swf.db import Database
from swf.egress import EgressConfig, EgressInfo
from swf.sandbox import open_task_sandbox
from swf.worker import EnvRequest, LocalCopyWorkspace, RunnerDeps, Worker, WorkerConfig

pytest_plugins = ["m4_fakes", "m5_fakes"]
pytestmark = [pytest.mark.docker, pytest.mark.network]

SEED_01 = "01-paginate-off-by-one"
SEED_02 = "02-mutable-default"
WRITE_NOTES = "python -c \"open('notes.txt','w').write('x')\""


def leftovers(run_id: str) -> list[str]:
    """Managed containers/networks of this test run (task label `swf-test-<run>-…`)."""
    found: list[str] = []
    for args in (["ps", "-a"], ["network", "ls"]):
        out = subprocess.run(
            [
                "docker",
                *args,
                "--filter",
                "label=swf.managed=1",
                "--format",
                '{{.Label "swf.task"}}',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        found += [
            line.strip()
            for line in out.stdout.splitlines()
            if line.strip().startswith(f"swf-test-{run_id}-")
        ]
    return found


def run_seed(
    seed: str,
    steps: list[Step],
    *,
    db: Database,
    egress_config: EgressConfig,
    run_id: str,
    tmp_path: Path,
    decider: AutoDecider,
    plant_pycache: bool = False,
) -> tuple[store.Task, str, Path]:
    source = tmp_path / "source"
    shutil.copytree(SEEDS / seed / "repo", source)
    if plant_pycache:
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "planted.cpython-312.pyc").write_bytes(b"stale bytecode")
    workspaces = tmp_path / "workspaces"

    def env_factory(request: EnvRequest) -> Any:
        named = dataclasses.replace(
            request, task_id=f"swf-test-{run_id}-{request.task_id[:12]}", worker_id=f"w-{run_id}"
        )
        return open_task_sandbox(egress_config, named)

    config = WorkerConfig(
        worker_id=f"w-{run_id}",
        workspaces_root=workspaces,
        agent={"poll_seconds": 0.05, "step_limit": 60},
    )
    deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(source, workspaces),
        env_factory=env_factory,
        model_factory=lambda: ScriptedModel(steps),
        config=config,
        evidence=None,
    )
    task = queued_task(
        db,
        failing_test=seed_target(seed),
        description=seed_description(seed),
        memory_mb=512,
        cpus=0.5,
    )
    worker = Worker(deps, orphan_cleaner=lambda *args, **kwargs: [])
    with decider:
        ran = worker.run_once()
    assert ran == task.id
    return task, str(task_row(db, task.id)["status"]), workspaces / str(task.id)


def diff_files(db: Database, task: store.Task) -> set[str]:
    diff = next(r for r in evidence_rows(db, task.id) if r.kind == "diff")
    assert diff.output is not None
    return {line[len("--- a/") :] for line in diff.output.splitlines() if line.startswith("--- a/")}


@pytest.mark.parametrize("seed", [SEED_01, SEED_02])
def test_a_seeded_task_completes_with_a_verified_evidence_bundle(
    seed: str,
    fresh_db: Database,
    egress: EgressInfo,
    egress_config: EgressConfig,
    run_id: str,
    tmp_path: Path,
) -> None:
    task, status, root = run_seed(
        seed,
        reference_steps(seed),
        db=fresh_db,
        egress_config=egress_config,
        run_id=run_id,
        tmp_path=tmp_path,
        decider=AutoDecider(fresh_db),
        plant_pycache=True,
    )
    assert status == "completed", task_row(fresh_db, task.id)["error"]
    rows = evidence_rows(fresh_db, task.id)
    assert [r.kind for r in rows] == [
        "baseline",
        "baseline_suite",
        "post",
        "suite",
        "diff",
        "harness_rerun",
    ]
    by_kind = {r.kind: r for r in rows}
    assert by_kind["baseline"].exit_code not in (0, None)
    assert seed_target(seed) in by_kind["baseline"].failures
    assert by_kind["baseline_suite"].exit_code not in (0, None)
    assert [by_kind[k].exit_code for k in ("post", "suite", "harness_rerun")] == [0, 0, 0]
    assert by_kind["harness_rerun"].details["passed"] is True
    assert diff_files(fresh_db, task) == set(seed_reference_files(seed))
    assert not list((root / "repo").rglob("__pycache__"))  # the planted bytecode is gone
    assert (root / "baseline").is_dir()
    assert leftovers(run_id) == []


def test_a_real_write_in_investigate_that_the_user_rejects_fails_the_task(
    fresh_db: Database,
    egress: EgressInfo,
    egress_config: EgressConfig,
    run_id: str,
    tmp_path: Path,
) -> None:
    steps: list[Step] = [
        [call("read_file", "read1", path="pager.py")],
        [call("bash", "w1", command=WRITE_NOTES)],
        [call("submit_plan", "plan1", **PLAN_ARGS)],
    ]
    task, status, root = run_seed(
        SEED_01,
        steps,
        db=fresh_db,
        egress_config=egress_config,
        run_id=run_id,
        tmp_path=tmp_path,
        decider=AutoDecider(fresh_db, reject_kinds=["readonly_violation"]),
    )
    assert status == "failed"
    assert task_row(fresh_db, task.id)["error"] == "readonly_violation"
    (violation,) = events_of_type(fresh_db, task.id, "backstop_violation")
    assert violation["added"] == ["notes.txt"]
    assert approvals_of(fresh_db, task.id) == [
        ("command", "approved"),
        ("readonly_violation", "rejected"),
    ]
    assert [r.kind for r in evidence_rows(fresh_db, task.id)] == ["baseline", "baseline_suite"]
    assert (root / "repo" / "notes.txt").is_file()  # the write really happened
    assert leftovers(run_id) == []


def test_a_real_write_in_investigate_that_the_user_approves_is_not_part_of_the_fix(
    fresh_db: Database,
    egress: EgressInfo,
    egress_config: EgressConfig,
    run_id: str,
    tmp_path: Path,
) -> None:
    reference = reference_steps(SEED_02)
    steps: list[Step] = [reference[0], [call("bash", "w1", command=WRITE_NOTES)], *reference[1:]]
    task, status, _ = run_seed(
        SEED_02,
        steps,
        db=fresh_db,
        egress_config=egress_config,
        run_id=run_id,
        tmp_path=tmp_path,
        decider=AutoDecider(fresh_db),
    )
    assert status == "completed", task_row(fresh_db, task.id)["error"]
    assert diff_files(fresh_db, task) == set(seed_reference_files(SEED_02))
    rows = evidence_rows(fresh_db, task.id)
    assert [bool(r.details.get("rebaseline")) for r in rows[:4]] == [False, False, True, True]
    assert [k for k, _ in approvals_of(fresh_db, task.id)] == [
        "command",
        "readonly_violation",
        "plan",
    ]
    assert leftovers(run_id) == []
