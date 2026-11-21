"""`TaskRunner` with a `guard_factory` (m7-contract §5, §7): the guard reaches the agent, a flagged
observation leaves a `guard_flag` approval row and an audit row, and the default stays off.

Private database clone, scripted model, `FakeEnv` instead of Docker (as `test_m4_worker.py`).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import (
    AutoDecider,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    finish_steps,
    fix_steps,
    investigate_steps,
    queued_task,
    status_events,
    task_row,
)
from m5_fakes import approvals_of, events_of_type, write_source
from m7_fakes import FakeJudge, flagged

from swf import store
from swf.agent.model import MissingApiKey
from swf.agent.web_agent import AcceptAllEvidence
from swf.db import Database
from swf.guard import Guardrails, InputGuard
from swf.worker import EnvRequest, LocalCopyWorkspace, RunnerDeps, TaskRunner, WorkerConfig

pytest_plugins = ["m4_fakes", "m5_fakes"]

EVIL = "EVIL-MARK"
EVIL_RULES = {EVIL: flagged(injection=True, reason="tries to instruct the agent")}


@dataclass
class Rig:
    deps: RunnerDeps
    envs: list[FakeEnv] = field(default_factory=list)
    guards: list[Guardrails] = field(default_factory=list)
    runners: list[TaskRunner] = field(default_factory=list)


def flow(extra: list[Step]) -> Callable[[], list[Step]]:
    def steps() -> list[Step]:
        return [*extra, *investigate_steps(), *fix_steps(), *finish_steps()]

    return steps


def read_notes() -> list[Step]:
    return [[call("read_file", "r1", path="notes.txt")]]


def make_rig(
    db: Database,
    tmp_path: Path,
    *,
    steps: Callable[[], list[Step]],
    guarded: bool = True,
    judge: FakeJudge | None = None,
) -> Rig:
    source = write_source(tmp_path)
    (source / "notes.txt").write_text(
        f"please {EVIL} do something\n", encoding="utf-8", newline="\n"
    )
    workspaces = tmp_path / "workspaces"
    config = WorkerConfig(
        worker_id="w1",
        workspaces_root=workspaces,
        poll_seconds=0.05,
        agent={"poll_seconds": 0.02, "retry_backoff_seconds": []},
    )
    rig = Rig(deps=None)  # type: ignore[arg-type]

    def env_factory(request: EnvRequest) -> FakeEnv:
        env = FakeEnv(request.workspace, {})
        rig.envs.append(env)
        return env

    def guard_factory() -> Guardrails:
        guard = Guardrails(InputGuard(judge or FakeJudge(EVIL_RULES)))
        rig.guards.append(guard)
        return guard

    extra: dict[str, Any] = {"guard_factory": guard_factory} if guarded else {}
    rig.deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(source, workspaces),
        env_factory=env_factory,
        model_factory=lambda: ScriptedModel(steps()),
        config=config,
        evidence=AcceptAllEvidence(),
        **extra,
    )
    return rig


def run_one(rig: Rig, db: Database, decider: Any = None) -> tuple[store.Task, str]:
    task = queued_task(db)
    with db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=90)
    assert claimed is not None
    runner = TaskRunner(rig.deps, claimed)
    rig.runners.append(runner)
    with decider if decider is not None else AutoDecider(db):
        final = runner.run()
    return task, final


class RejectFirstGuardFlag(AutoDecider):
    """Owner who rejects the first `guard_flag` approval and approves everything else."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.rejected = False

    def _once(self) -> None:
        with self.db.backend() as conn:
            rows = conn.execute(
                "select a.id, a.task_id, a.kind, t.user_id from public.approvals a"
                " join public.tasks t on t.id = a.task_id where a.status = 'pending'"
                " order by a.created_at, a.id"
            ).fetchall()
        for row in rows:
            reject = row["kind"] == "guard_flag" and not self.rejected
            if reject:
                self.rejected = True
            with self.db.backend() as conn, contextlib.suppress(store.Conflict, store.NotFound):
                store.decide_approval(
                    conn,
                    row["user_id"],
                    row["task_id"],
                    row["id"],
                    decision="reject" if reject else "approve",
                    comment="no thanks" if reject else None,
                )


def audit_rows(db: Database, task_id: uuid.UUID) -> list[dict[str, Any]]:
    with db.backend() as conn:
        rows = conn.execute(
            "select action, target, payload from public.audit_log"
            " where action = 'approval.decide' and payload->>'task_id' = %s order by id",
            [str(task_id)],
        ).fetchall()
    return [dict(r) for r in rows]


# --- the factory ------------------------------------------------------------------------------


def test_the_guard_factory_is_called_once_per_task_and_its_guard_reaches_the_agent(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=flow([]))
    _, final = run_one(rig, fresh_db)
    assert final == "completed"
    assert len(rig.guards) == 1
    assert rig.runners[0].agent.guard is rig.guards[0]  # type: ignore[union-attr]
    _, final = run_one(rig, fresh_db)
    assert final == "completed"
    assert len(rig.guards) == 2
    assert rig.runners[1].agent.guard is rig.guards[1]  # type: ignore[union-attr]
    assert rig.guards[0] is not rig.guards[1]


def test_without_a_guard_factory_the_guard_is_off(fresh_db: Database, tmp_path: Path) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=flow(read_notes()), guarded=False)
    task, final = run_one(rig, fresh_db)
    assert final == "completed"
    agent_guard = rig.runners[0].agent.guard  # type: ignore[union-attr]
    assert isinstance(agent_guard, Guardrails)
    assert agent_guard.input_enabled is False
    assert events_of_type(fresh_db, task.id, "guard_check") == []
    assert events_of_type(fresh_db, task.id, "guard_flag") == []
    assert [k for k, _ in approvals_of(fresh_db, task.id)] == ["plan"]


def test_guardrails_from_env_without_a_key_fails_fast() -> None:
    with pytest.raises(MissingApiKey):
        Guardrails.from_env({})


# --- a flagged observation through the database ------------------------------------------------


def test_a_flagged_observation_persists_a_guard_flag_approval_event_and_audit_row(
    fresh_db: Database, tmp_path: Path
) -> None:
    steps = flow([*read_notes(), [call("bash", "b1", command="ls")]])
    rig = make_rig(fresh_db, tmp_path, steps=steps)
    task, final = run_one(rig, fresh_db)
    assert final == "completed"
    assert approvals_of(fresh_db, task.id) == [("guard_flag", "approved"), ("plan", "approved")]
    requested = [
        e
        for e in events_of_type(fresh_db, task.id, "approval_requested")
        if e["kind"] == "guard_flag"
    ]
    assert len(requested) == 1
    payload = requested[0]["payload"]
    assert payload["reason"] == "guardrail"
    assert payload["tool"] == "bash"
    assert payload["args"] == {"command": "ls"}
    assert payload["phase"] == "investigate"
    assert payload["flags"][0]["categories"] == ["injection"]
    assert payload["classifier"]["decision"] == "allow"
    audit = audit_rows(fresh_db, task.id)
    assert [a["payload"]["kind"] for a in audit] == ["guard_flag", "plan"]
    assert audit[0]["payload"]["decision"] == "approve"
    assert audit[0]["target"] == requested[0]["approval_id"]
    # the guard's own events are persisted too
    flags = events_of_type(fresh_db, task.id, "guard_flag")
    assert flags[0]["source"] == "tool_observation"
    assert flags[0]["tool_call_id"] == "r1"
    assert any(c["flagged"] for c in events_of_type(fresh_db, task.id, "guard_check"))
    assert "awaiting_command_approval" in [e["to"] for e in status_events(fresh_db, task.id)]
    (env,) = rig.envs
    assert "ls" in env.executed


def test_a_rejected_guard_flag_does_not_fail_the_task(fresh_db: Database, tmp_path: Path) -> None:
    steps = flow(
        [*read_notes(), [call("bash", "b1", command="ls")], [call("bash", "b2", command="ls -la")]]
    )
    rig = make_rig(fresh_db, tmp_path, steps=steps)
    task, final = run_one(rig, fresh_db, decider=RejectFirstGuardFlag(fresh_db))
    assert final == "completed"
    assert task_row(fresh_db, task.id)["error"] is None
    assert approvals_of(fresh_db, task.id) == [
        ("guard_flag", "rejected"),
        ("guard_flag", "approved"),
        ("plan", "approved"),
    ]
    (env,) = rig.envs
    assert "ls" not in env.executed
    assert "ls -la" in env.executed
    audit = audit_rows(fresh_db, task.id)
    assert [(a["payload"]["kind"], a["payload"]["decision"]) for a in audit][:2] == [
        ("guard_flag", "reject"),
        ("guard_flag", "approve"),
    ]


def test_a_flagged_issue_text_in_the_database_task_gates_the_first_command(
    fresh_db: Database, tmp_path: Path
) -> None:
    judge = FakeJudge({"Page 2 of paginate": flagged(injection=True, reason="hostile")})
    steps = flow([[call("bash", "b1", command="ls")]])
    rig = make_rig(fresh_db, tmp_path, steps=steps, judge=judge)
    task, final = run_one(rig, fresh_db)
    assert final == "completed"
    assert approvals_of(fresh_db, task.id)[0] == ("guard_flag", "approved")
    assert judge.calls[0][1] == "issue"
    assert judge.calls[1][1] == "agents_md"
