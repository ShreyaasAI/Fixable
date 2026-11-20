"""Claim, leases, reconcile, `TaskRunner` and `Worker` (m4-contract §7, §8, A2, A3).

Everything runs against a private database clone and fakes (scripted model, fake environment);
waits that need a second party use a background `AutoDecider` playing the task owner.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from m4_fakes import (
    FAILING_TEST,
    SEED_REPO,
    TEST_COMMAND,
    AutoDecider,
    FakeClock,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    happy_steps,
    investigate_steps,
    make_db_user,
    queued_task,
    status_events,
    task_owner,
    task_row,
    wait_until,
)
from minisweagent.exceptions import FormatError

from swf import policy, store
from swf.agent.web_agent import AcceptAllEvidence
from swf.db import Database
from swf.preflight import Limits, Reservation, fits
from swf.worker import (
    EnvRequest,
    LocalCopyWorkspace,
    RunnerDeps,
    TaskRunner,
    TaskWorkspace,
    Worker,
    WorkerConfig,
)

pytest_plugins = ["m4_fakes"]

TERMINAL = set(store.TERMINAL_STATUSES)
PIP = "python -m pip install --quiet pytest==9.2.0"
ROOMY = Limits(max_concurrent=10, host_memory_mb=1_000_000, host_cpus=1000.0)


# --- rig ------------------------------------------------------------------------------------


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0


class TrackedEnv(FakeEnv):
    def __init__(self, root: Path, bash: Any, tracker: Tracker) -> None:
        super().__init__(root, bash)
        self.tracker = tracker

    def cleanup(self) -> None:
        super().cleanup()
        with self.tracker.lock:
            self.tracker.active -= 1


@dataclass
class Rig:
    deps: RunnerDeps
    envs: list[TrackedEnv] = field(default_factory=list)
    requests: list[EnvRequest] = field(default_factory=list)
    tracker: Tracker = field(default_factory=Tracker)
    clock: FakeClock | None = None

    @property
    def worker_id(self) -> str:
        return self.deps.config.worker_id


def write_source(tmp_path: Path, *, setup: tuple[str, ...] = (), name: str = "source") -> Path:
    """A copy of seed 01 whose AGENTS.md declares `setup`."""
    source = tmp_path / name
    if source.exists():
        return source
    shutil.copytree(SEED_REPO, source)
    config = {
        "image": "python:3.12-slim",
        "setup": list(setup),
        "test": TEST_COMMAND,
        "allow": ["python -m pytest*"],
        "resources": {"memory_mb": 512, "cpus": 0.5},
    }
    text = "# Pager\n\nRun the tests with pytest.\n\n```yaml agent-config\n"
    text += yaml.safe_dump(config) + "```\n"
    (source / "AGENTS.md").write_text(text, encoding="utf-8", newline="\n")
    return source


def make_rig(
    db: Database,
    tmp_path: Path,
    *,
    steps: Callable[[], list[Step]] | None = None,
    model_factory: Callable[[], Any] | None = None,
    setup: tuple[str, ...] = (),
    bash: Any = None,
    worker_id: str = "w1",
    agent: dict[str, Any] | None = None,
    fake_time: bool = False,
    start_failures: int = 0,
    tracker: Tracker | None = None,
    create_delay: float = 0.0,
    limits: Limits = Limits(),  # noqa: B008
    lease_seconds: int = 90,
    heartbeat_seconds: int = 30,
    max_concurrent: int = 2,
    evidence: Any = AcceptAllEvidence(),  # noqa: B008 - explicit M4 opt-out (m5 §0.6)
) -> Rig:
    rig = Rig(deps=None, tracker=tracker or Tracker())  # type: ignore[arg-type]
    failures = {"left": start_failures}

    def env_factory(request: EnvRequest) -> TrackedEnv:
        rig.requests.append(request)
        with rig.tracker.lock:
            rig.tracker.active += 1
            rig.tracker.peak = max(rig.tracker.peak, rig.tracker.active)
        if create_delay:
            time.sleep(create_delay)
        if failures["left"] > 0:
            failures["left"] -= 1
            with rig.tracker.lock:
                rig.tracker.active -= 1
            raise RuntimeError("docker is down")
        env = TrackedEnv(request.workspace, bash, rig.tracker)
        rig.envs.append(env)
        return env

    def default_model() -> ScriptedModel:
        return ScriptedModel(steps() if steps is not None else happy_steps())

    config = WorkerConfig(
        worker_id=worker_id,
        workspaces_root=tmp_path / "workspaces",
        max_concurrent=max_concurrent,
        poll_seconds=0.05,
        limits=limits,
        agent=agent if agent is not None else ({} if fake_time else {"poll_seconds": 0.02}),
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    extra: dict[str, Any] = {}
    if fake_time:
        rig.clock = FakeClock()
        extra = {"clock": rig.clock, "sleep": rig.clock.sleep}
    extra["evidence"] = evidence
    rig.deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(write_source(tmp_path, setup=setup), config.workspaces_root),
        env_factory=env_factory,
        model_factory=model_factory or default_model,
        config=config,
        **extra,
    )
    return rig


def claim(db: Database, worker_id: str = "w1", *, lease: int = 90, **kwargs: Any) -> store.Task:
    with db.backend() as conn:
        task = store.claim_next_task(conn, worker_id=worker_id, lease_seconds=lease, **kwargs)
    assert task is not None
    return task


def runner_for(rig: Rig, task: store.Task) -> TaskRunner:
    return TaskRunner(rig.deps, task)


def in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    result: list[Any] = []

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def status_of(db: Database, task_id: uuid.UUID) -> str:
    return str(task_row(db, task_id)["status"])


def finished(db: Database, task_id: uuid.UUID) -> Callable[[], bool]:
    return lambda: status_of(db, task_id) in TERMINAL


def event_payloads(db: Database, task_id: uuid.UUID, type: str) -> list[dict[str, Any]]:  # noqa: A002
    with db.backend() as conn:
        rows = conn.execute(
            "select payload from public.events where task_id = %s and type = %s order by seq",
            [task_id, type],
        ).fetchall()
    return [dict(r["payload"]) for r in rows]


def set_active(
    db: Database,
    task_id: uuid.UUID,
    *,
    worker_id: str,
    lease: float | None,
    status: str = "executing",
) -> None:
    """Test setup: make a row look claimed by `worker_id` with a lease `lease` seconds from now."""
    with db.backend() as conn:
        conn.execute(
            "update public.tasks set status = %s, worker_id = %s,"
            " lease_expires_at = case when %s::float8 is null then null"
            " else now() + %s::float8 * interval '1 second' end where id = %s",
            [status, worker_id, lease, lease, task_id],
        )


class Cleaner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
        active = kwargs.get("active_task_ids", args[0] if args else None)
        assert active is not None
        self.calls.append(
            {
                "active": {str(a) for a in active},
                "worker_id": kwargs.get("worker_id"),
                "reap": {str(r) for r in kwargs.get("reap_task_ids", ())},
            }
        )
        return []


# --- fits -----------------------------------------------------------------------------------


def test_fits_applies_the_concurrency_memory_and_cpu_rules() -> None:
    small = Reservation(512, 0.5)
    assert fits([], small)
    assert fits([small], small)
    assert not fits([small, small], small)  # two running already
    assert not fits([Reservation(4096, 0.5)], Reservation(4096, 0.5), Limits(host_memory_mb=8000))
    assert fits([Reservation(4096, 0.5)], Reservation(4096, 0.5))  # exactly the host memory
    assert not fits([Reservation(512, 1.5)], Reservation(512, 1.0))  # 2.5 vCPU > 2
    assert fits([Reservation(512, 1.0)], Reservation(512, 1.0))  # exactly 2 vCPU


# --- claim ----------------------------------------------------------------------------------


def test_claim_takes_the_oldest_queued_task_and_records_the_lease(fresh_db: Database) -> None:
    first = queued_task(fresh_db)
    second = queued_task(fresh_db)
    third = queued_task(fresh_db)
    a = claim(fresh_db, "w1", lease=60)
    assert a.id == first.id
    assert (a.status, a.phase, a.worker_id) == ("preparing", "prepare", "w1")
    assert a.claimed_at is not None
    with fresh_db.backend() as conn:
        row = conn.execute(
            "select lease_expires_at > now() as live, heartbeat_at is not null as beat,"
            " lease_expires_at - heartbeat_at as span from public.tasks where id = %s",
            [first.id],
        ).fetchone()
    assert row is not None
    assert row["live"] is True
    assert row["beat"] is True
    assert 50 <= row["span"].total_seconds() <= 70
    assert claim(fresh_db, "w2").id == second.id
    with fresh_db.backend() as conn:  # two tasks already run: the third waits
        assert store.claim_next_task(conn, worker_id="w1", lease_seconds=60) is None
    assert status_of(fresh_db, third.id) == "queued"
    last = status_events(fresh_db, first.id)[-1]
    assert (last["from"], last["to"]) == ("queued", "preparing")


def test_claim_returns_none_when_nothing_is_queued(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        assert store.claim_next_task(conn, worker_id="w1", lease_seconds=60) is None


def test_two_claimers_never_take_the_same_task(fresh_db: Database) -> None:
    ids = {queued_task(fresh_db, memory_mb=256, cpus=0.25).id for _ in range(6)}
    taken: dict[str, list[uuid.UUID]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def claimer(name: str) -> None:
        barrier.wait()
        while True:
            with fresh_db.backend() as conn:
                task = store.claim_next_task(conn, worker_id=name, lease_seconds=60, limits=ROOMY)
            if task is None:
                return
            taken[name].append(task.id)

    threads = [threading.Thread(target=claimer, args=(n,)) for n in taken]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    everything = taken["a"] + taken["b"]
    assert sorted(everything) == sorted(ids)
    assert len(set(everything)) == len(everything)


def test_with_room_for_one_more_two_claimers_admit_exactly_one(fresh_db: Database) -> None:
    running = queued_task(fresh_db)
    claim(fresh_db, "w0")
    assert status_of(fresh_db, running.id) == "preparing"
    queued_task(fresh_db)
    queued_task(fresh_db)
    results: list[store.Task | None] = []
    barrier = threading.Barrier(2)

    def claimer(name: str) -> None:
        barrier.wait()
        with fresh_db.backend() as conn:
            results.append(store.claim_next_task(conn, worker_id=name, lease_seconds=60))

    threads = [threading.Thread(target=claimer, args=(n,)) for n in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 2
    assert len([r for r in results if r is not None]) == 1


def test_a_head_that_does_not_fit_blocks_the_queue(fresh_db: Database) -> None:
    limits = Limits(max_concurrent=3, host_memory_mb=1000, host_cpus=10.0)
    busy = queued_task(fresh_db, memory_mb=600, cpus=0.5)
    head = queued_task(fresh_db, memory_mb=600, cpus=0.5)  # 600 + 600 > 1000
    small = queued_task(fresh_db, memory_mb=100, cpus=0.5)  # would fit, but must not overtake
    assert claim(fresh_db, "w1", limits=limits).id == busy.id
    with fresh_db.backend() as conn:
        assert store.claim_next_task(conn, worker_id="w1", lease_seconds=60, limits=limits) is None
    assert status_of(fresh_db, small.id) == "queued"
    with fresh_db.backend() as conn:
        store.transition_task(conn, busy.id, "failed", error="done")
    assert claim(fresh_db, "w1", limits=limits).id == head.id


def test_renew_leases_advances_the_lease_and_leaves_other_workers_alone(
    fresh_db: Database,
) -> None:
    queued_task(fresh_db)
    queued_task(fresh_db)
    mine = claim(fresh_db, "w1", lease=10)
    theirs = claim(fresh_db, "w2", lease=10)
    before = task_row(fresh_db, mine.id)
    other_before = task_row(fresh_db, theirs.id)
    with fresh_db.backend() as conn:
        renewed = store.renew_leases(
            conn, worker_id="w1", task_ids=[mine.id, theirs.id], lease_seconds=500
        )
    assert renewed == [mine.id]
    after = task_row(fresh_db, mine.id)
    assert after["lease_expires_at"] > before["lease_expires_at"]
    assert after["heartbeat_at"] >= before["heartbeat_at"]
    assert task_row(fresh_db, theirs.id)["lease_expires_at"] == other_before["lease_expires_at"]


# --- reconcile: store.interrupt_orphans (A2) -------------------------------------------------


def orphans(db: Database, *, worker_id: str = "w1", restart: bool = False) -> list[Any]:
    with db.backend() as conn:
        return store.interrupt_orphans(conn, worker_id=worker_id, restart=restart)


def test_another_workers_expired_lease_is_interrupted(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    set_active(fresh_db, task.id, worker_id="other", lease=-30)
    assert orphans(fresh_db) == [(task.id, "other")]
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("interrupted", "lease_expired")
    assert row["finished_at"] is not None
    last = status_events(fresh_db, task.id)[-1]
    assert (last["from"], last["to"], last["error"]) == (
        "executing",
        "interrupted",
        "lease_expired",
    )


def test_another_workers_live_lease_is_left_alone(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    set_active(fresh_db, task.id, worker_id="other", lease=300)
    assert orphans(fresh_db) == []
    assert status_of(fresh_db, task.id) == "executing"


def test_an_active_row_without_a_lease_counts_as_expired(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    set_active(fresh_db, task.id, worker_id="other", lease=None)
    assert orphans(fresh_db) == [(task.id, "other")]
    assert task_row(fresh_db, task.id)["error"] == "lease_expired"


def test_restart_interrupts_own_rows_even_with_a_live_lease(fresh_db: Database) -> None:
    mine = queued_task(fresh_db)
    theirs = queued_task(fresh_db)
    set_active(fresh_db, mine.id, worker_id="w1", lease=300)
    set_active(fresh_db, theirs.id, worker_id="w2", lease=300)
    assert orphans(fresh_db, worker_id="w1", restart=False) == []
    assert status_of(fresh_db, mine.id) == "executing"
    assert orphans(fresh_db, worker_id="w1", restart=True) == [(mine.id, "w1")]
    row = task_row(fresh_db, mine.id)
    assert (row["status"], row["error"]) == ("interrupted", "worker_restarted")
    assert status_of(fresh_db, theirs.id) == "executing"


def test_interrupting_expires_pending_approvals_and_open_questions(fresh_db: Database) -> None:
    task = queued_task(fresh_db)
    with fresh_db.backend() as conn:
        approval = store.create_approval(conn, task.id, "plan", {})
        question = store.create_question(conn, task.id, "q?")
    set_active(fresh_db, task.id, worker_id="other", lease=-1, status="awaiting_plan_approval")
    orphans(fresh_db)
    with fresh_db.backend() as conn:
        a, q = store.get_approval(conn, approval.id), store.get_question(conn, question.id)
    assert a is not None
    assert a.status == "expired"
    assert q is not None
    assert q.expired_at is not None


def test_only_active_rows_are_considered(fresh_db: Database) -> None:
    waiting = queued_task(fresh_db)
    done = queued_task(fresh_db, status="completed")
    with fresh_db.backend() as conn:  # a queued row and a terminal row, both with old leases
        conn.execute(
            "update public.tasks set worker_id = 'other',"
            " lease_expires_at = now() - interval '1 hour' where id = any(%s)",
            [[waiting.id, done.id]],
        )
    assert orphans(fresh_db, restart=True) == []
    assert status_of(fresh_db, waiting.id) == "queued"
    assert status_of(fresh_db, done.id) == "completed"


def test_worker_reconcile_interrupts_and_hands_the_cleaner_the_right_arguments(
    fresh_db: Database, tmp_path: Path
) -> None:
    stale = queued_task(fresh_db)
    live = queued_task(fresh_db)
    set_active(fresh_db, stale.id, worker_id="other", lease=-30)
    set_active(fresh_db, live.id, worker_id="other", lease=300)
    cleaner = Cleaner()
    worker = Worker(make_rig(fresh_db, tmp_path, worker_id="w1").deps, orphan_cleaner=cleaner)
    interrupted = worker.reconcile(restart=False)
    assert [str(i) for i in interrupted] == [str(stale.id)]
    (seen,) = cleaner.calls
    assert seen["active"] == set()  # this worker runs nothing
    assert seen["worker_id"] == "w1"
    assert seen["reap"] == {str(stale.id)}
    assert status_of(fresh_db, live.id) == "executing"


def test_worker_reconcile_on_restart_reaps_its_own_previous_tasks(
    fresh_db: Database, tmp_path: Path
) -> None:
    mine = queued_task(fresh_db)
    set_active(fresh_db, mine.id, worker_id="w1", lease=300)
    cleaner = Cleaner()
    worker = Worker(make_rig(fresh_db, tmp_path, worker_id="w1").deps, orphan_cleaner=cleaner)
    assert [str(i) for i in worker.reconcile(restart=True)] == [str(mine.id)]
    assert cleaner.calls[0]["reap"] == {str(mine.id)}
    assert task_row(fresh_db, mine.id)["error"] == "worker_restarted"


# --- configuration --------------------------------------------------------------------------


def test_worker_config_defaults_and_the_heartbeat_rule(tmp_path: Path) -> None:
    config = WorkerConfig(worker_id="w1", workspaces_root=tmp_path)
    assert (config.max_concurrent, config.poll_seconds) == (2, 2.0)
    assert (config.lease_seconds, config.heartbeat_seconds) == (90, 30)
    WorkerConfig(worker_id="w1", workspaces_root=tmp_path, lease_seconds=90, heartbeat_seconds=44)
    with pytest.raises(ValueError):
        WorkerConfig(
            worker_id="w1", workspaces_root=tmp_path, lease_seconds=90, heartbeat_seconds=45
        )
    with pytest.raises(ValueError):
        WorkerConfig(
            worker_id="w1", workspaces_root=tmp_path, lease_seconds=10, heartbeat_seconds=30
        )


def test_local_copy_workspace_copies_the_source_per_task(
    fresh_db: Database, tmp_path: Path
) -> None:
    source = write_source(tmp_path)
    provider = LocalCopyWorkspace(source, tmp_path / "workspaces")
    one = queued_task(fresh_db)
    two = queued_task(fresh_db)
    ws_one = provider.prepare(one)
    ws_two = provider.prepare(two)
    assert isinstance(ws_one, TaskWorkspace)
    assert ws_one.root == tmp_path / "workspaces" / str(one.id)
    assert ws_one.repo == ws_one.root / "repo"
    assert (ws_one.repo / "pager.py").is_file()
    (ws_one.repo / "pager.py").write_text("changed", encoding="utf-8")
    assert (ws_two.repo / "pager.py").read_text(encoding="utf-8") != "changed"
    assert (source / "pager.py").read_text(encoding="utf-8") != "changed"


# --- TaskRunner: the whole flow -------------------------------------------------------------


def test_a_task_runs_through_the_database_to_completed(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, steps=happy_steps)
    claimed = claim(fresh_db, rig.worker_id)
    with AutoDecider(fresh_db):
        final = runner_for(rig, claimed).run()
    assert final == "completed"
    row = task_row(fresh_db, task.id)
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert row["error"] is None
    assert row["lease_expires_at"] is None
    assert [e["to"] for e in status_events(fresh_db, task.id)] == [
        "queued",
        "preparing",
        "investigating",
        "awaiting_plan_approval",
        "executing",
        "verifying",
        "confirming",
        "completed",
    ]
    assert (tmp_path / "workspaces" / str(task.id) / "trajectory.json").is_file()
    trajectory = json.loads(
        (tmp_path / "workspaces" / str(task.id) / "trajectory.json").read_text(encoding="utf-8")
    )
    assert trajectory["info"]["exit_status"] == "Submitted"
    (env,) = rig.envs
    assert env.cleanups == 1
    assert rig.tracker.active == 0


def test_the_environment_request_comes_from_the_task_and_the_worker(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db, memory_mb=768, cpus=0.75, image="python:3.12-slim")
    rig = make_rig(fresh_db, tmp_path, worker_id="host-a")
    with AutoDecider(fresh_db):
        runner_for(rig, claim(fresh_db, "host-a")).run()
    (request,) = rig.requests
    assert request.task_id == str(task.id)
    assert request.image == "python:3.12-slim"
    assert request.memory_mb == 768
    assert request.cpus == pytest.approx(0.75)
    assert request.worker_id == "host-a"
    assert request.workspace.is_dir()
    assert (request.workspace / "pager.py").is_file()


def test_the_agent_gets_the_task_failing_test_and_the_agents_md_test_command(
    fresh_db: Database, tmp_path: Path
) -> None:
    queued_task(fresh_db, failing_test=FAILING_TEST)
    rig = make_rig(fresh_db, tmp_path)
    with AutoDecider(fresh_db):
        assert runner_for(rig, claim(fresh_db, rig.worker_id)).run() == "completed"
    (env,) = rig.envs
    assert [c for c in env.executed if "pytest" in c] == [f"{TEST_COMMAND} {FAILING_TEST}"]


def test_model_cost_is_added_to_the_task(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(
        fresh_db,
        tmp_path,
        model_factory=lambda: ScriptedModel(happy_steps(), cost=0.01),
    )
    with AutoDecider(fresh_db):
        runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert float(task_row(fresh_db, task.id)["cost_usd"]) == pytest.approx(0.05)


# --- TaskRunner: setup and start failures ---------------------------------------------------


def failing_pip(failures: int) -> tuple[Callable[[str], dict[str, Any]], dict[str, int]]:
    seen = {"n": 0}

    def bash(command: str) -> dict[str, Any]:
        if command == PIP:
            seen["n"] += 1
            code = 1 if seen["n"] <= failures else 0
            return {
                "output": "pip says no" if code else "",
                "returncode": code,
                "exception_info": "",
            }
        return {"output": "", "returncode": 0, "exception_info": ""}

    return bash, seen


def test_setup_runs_unattended_with_a_timeout_and_is_reported(
    fresh_db: Database, tmp_path: Path
) -> None:
    assert policy.classify(PIP, phase="prepare").decision != "forbid"
    task = queued_task(fresh_db)
    bash, _ = failing_pip(0)
    rig = make_rig(fresh_db, tmp_path, setup=(PIP,), bash=bash)
    with AutoDecider(fresh_db):
        assert runner_for(rig, claim(fresh_db, rig.worker_id)).run() == "completed"
    (env,) = rig.envs
    assert env.executed[0] == PIP
    assert env.timeouts[0] == 600
    (setup,) = event_payloads(fresh_db, task.id, "setup")
    assert (setup["command"], setup["attempt"], setup["returncode"]) == (PIP, 1, 0)
    with fresh_db.backend() as conn:
        kinds = [
            r["kind"]
            for r in conn.execute("select kind from public.approvals where task_id = %s", [task.id])
        ]
    assert kinds == ["plan"]  # the setup command never asked anybody


def test_a_failing_setup_command_asks_the_user_and_retry_continues(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    bash, seen = failing_pip(3)
    rig = make_rig(fresh_db, tmp_path, setup=(PIP,), bash=bash, fake_time=True)
    with AutoDecider(fresh_db, answer="retry"):
        final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "completed"
    assert seen["n"] == 4
    setups = event_payloads(fresh_db, task.id, "setup")
    assert [s["returncode"] for s in setups] == [1, 1, 1, 0]
    assert [s["attempt"] for s in setups[:3]] == [1, 2, 3]
    statuses = [e["to"] for e in status_events(fresh_db, task.id)]
    assert statuses[:4] == ["queued", "preparing", "awaiting_input", "preparing"]
    with fresh_db.backend() as conn:
        rows = conn.execute(
            "select source, answer from public.questions where task_id = %s", [task.id]
        ).fetchall()
    assert [(r["source"], r["answer"]) for r in rows] == [("system", "retry")]


def test_any_other_answer_to_a_failing_setup_fails_the_task(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    bash, seen = failing_pip(99)
    rig = make_rig(fresh_db, tmp_path, setup=(PIP,), bash=bash, fake_time=True)
    with AutoDecider(fresh_db, answer="stop"):
        final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "failed"
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "retries_exhausted")
    assert seen["n"] == 3
    assert rig.envs[0].cleanups == 1


def test_a_forbidden_setup_command_fails_the_task_without_running_it(
    fresh_db: Database, tmp_path: Path
) -> None:
    command = "sudo apt-get install -y git"
    assert policy.classify(command, phase="prepare").decision == "forbid"
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, setup=(command,))
    final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "failed"
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "setup_forbidden")
    assert rig.envs[0].executed == []
    assert rig.envs[0].cleanups == 1


def test_an_environment_that_fails_to_start_is_retried_then_asked_about(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, start_failures=3, fake_time=True)
    with AutoDecider(fresh_db, answer="retry"):
        final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "completed"
    assert len(rig.requests) == 4
    assert "awaiting_input" in [e["to"] for e in status_events(fresh_db, task.id)]


def test_giving_up_on_a_dead_environment_fails_the_task(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, start_failures=99, fake_time=True)
    with AutoDecider(fresh_db, answer="no"):
        final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "failed"
    assert (task_row(fresh_db, task.id)["error"]) == "retries_exhausted"
    assert len(rig.requests) == 3


# --- TaskRunner: exit mapping ---------------------------------------------------------------


def run_to_end(
    db: Database, tmp_path: Path, *, steps: list[Step] | Callable[[], list[Step]], **kw: Any
) -> tuple[store.Task, str, Rig]:
    task = queued_task(db)
    factory = steps if callable(steps) else (lambda: list(steps))
    rig = make_rig(db, tmp_path, steps=factory, **kw)
    final = runner_for(rig, claim(db, rig.worker_id)).run()
    return task, final, rig


def test_an_unanswered_plan_approval_fails_with_approval_timeout(
    fresh_db: Database, tmp_path: Path
) -> None:
    task, final, rig = run_to_end(
        fresh_db,
        tmp_path,
        steps=investigate_steps(),
        fake_time=True,
        agent={"approval_timeout_seconds": 20, "poll_seconds": 2.0},
    )
    assert final == "failed"
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "approval_timeout")
    with fresh_db.backend() as conn:
        approvals = conn.execute(
            "select status from public.approvals where task_id = %s", [task.id]
        ).fetchall()
    assert [a["status"] for a in approvals] == ["expired"]
    assert rig.envs[0].cleanups == 1
    assert (tmp_path / "workspaces" / str(task.id) / "trajectory.json").is_file()


def test_an_unanswered_question_fails_with_question_timeout(
    fresh_db: Database, tmp_path: Path
) -> None:
    task, final, _ = run_to_end(
        fresh_db,
        tmp_path,
        steps=[[call("ask_user", question="Which size?")]],
        fake_time=True,
        agent={"approval_timeout_seconds": 20, "poll_seconds": 2.0},
    )
    assert final == "failed"
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "question_timeout")
    with fresh_db.backend() as conn:
        question = conn.execute(
            "select answer, expired_at from public.questions where task_id = %s", [task.id]
        ).fetchone()
    assert question is not None
    assert question["answer"] is None
    assert question["expired_at"] is not None


def test_the_step_limit_fails_the_task_with_limits_exceeded(
    fresh_db: Database, tmp_path: Path
) -> None:
    task, final, _ = run_to_end(
        fresh_db,
        tmp_path,
        steps=[[call("read_file", path="pager.py")]] * 3,
        agent={"step_limit": 1, "poll_seconds": 0.02},
    )
    assert final == "failed"
    assert task_row(fresh_db, task.id)["error"] == "limits_exceeded"


def test_repeated_format_errors_fail_the_task(fresh_db: Database, tmp_path: Path) -> None:
    bad = FormatError(
        {"role": "user", "content": "bad", "extra": {"interrupt_type": "FormatError"}}
    )
    task, final, _ = run_to_end(fresh_db, tmp_path, steps=[bad, bad, bad])
    assert final == "failed"
    assert task_row(fresh_db, task.id)["error"] == "repeated_format_error"


def test_giving_up_on_a_failing_model_fails_the_task_with_retries_exhausted(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    steps: list[Step] = [RuntimeError("gateway down")] * 3
    rig = make_rig(fresh_db, tmp_path, steps=lambda: list(steps), fake_time=True)
    with AutoDecider(fresh_db, answer="no"):
        final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()
    assert final == "failed"
    assert task_row(fresh_db, task.id)["error"] == "retries_exhausted"


def test_an_unexpected_error_fails_the_task_and_still_cleans_up(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)

    def broken_model() -> ScriptedModel:
        raise ValueError("cannot build the model")

    rig = make_rig(fresh_db, tmp_path, model_factory=broken_model)
    final = runner_for(rig, claim(fresh_db, rig.worker_id)).run()  # never raises
    assert final == "failed"
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "internal_error: ValueError")
    assert rig.envs[0].cleanups == 1
    assert rig.tracker.active == 0


# --- TaskRunner: the API and the owner decide -----------------------------------------------


def test_an_approval_through_the_api_unblocks_the_runner(
    fresh_db: Database,
    fresh_client: TestClient,
    auth: Callable[..., dict[str, str]],
    tmp_path: Path,
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    rig = make_rig(fresh_db, tmp_path)
    claimed = claim(fresh_db, rig.worker_id)
    thread, result = in_thread(lambda: runner_for(rig, claimed).run())
    approval = wait_until(
        lambda: _pending(fresh_db, task.id, "approvals"), what="a pending plan approval"
    )
    assert status_of(fresh_db, task.id) == "awaiting_plan_approval"
    response = fresh_client.post(
        f"/v1/tasks/{task.id}/approvals/{approval}",
        json={"decision": "approve"},
        headers=auth(user),
    )
    assert response.status_code == 200
    thread.join(timeout=60)
    assert result == ["completed"]
    assert status_of(fresh_db, task.id) == "completed"


def test_an_answer_through_the_api_unblocks_a_question(
    fresh_db: Database,
    fresh_client: TestClient,
    auth: Callable[..., dict[str, str]],
    tmp_path: Path,
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    steps = [[call("ask_user", "ask1", question="Which size?")], *happy_steps()]
    rig = make_rig(fresh_db, tmp_path, steps=lambda: list(steps))
    claimed = claim(fresh_db, rig.worker_id)
    thread, result = in_thread(lambda: runner_for(rig, claimed).run())
    question = wait_until(lambda: _pending(fresh_db, task.id, "questions"), what="a question")
    wait_until(lambda: status_of(fresh_db, task.id) == "awaiting_input", what="awaiting_input")
    response = fresh_client.post(
        f"/v1/tasks/{task.id}/answers/{question}", json={"answer": "10"}, headers=auth(user)
    )
    assert response.status_code == 200
    with AutoDecider(fresh_db):  # the plan approval comes next
        thread.join(timeout=60)
    assert result == ["completed"]


def _pending(db: Database, task_id: uuid.UUID, table: str) -> uuid.UUID | None:
    where = (
        "status = 'pending'" if table == "approvals" else "answer is null and expired_at is null"
    )
    with db.backend() as conn:
        row = conn.execute(
            f"select id from public.{table} where task_id = %s and {where}", [task_id]
        ).fetchone()
    return None if row is None else uuid.UUID(str(row["id"]))


def test_cancelling_through_the_api_stops_the_runner(
    fresh_db: Database,
    fresh_client: TestClient,
    auth: Callable[..., dict[str, str]],
    tmp_path: Path,
) -> None:
    user = make_db_user(fresh_db)
    task = queued_task(fresh_db, user=user)
    rig = make_rig(fresh_db, tmp_path)
    claimed = claim(fresh_db, rig.worker_id)
    thread, result = in_thread(lambda: runner_for(rig, claimed).run())
    wait_until(lambda: _pending(fresh_db, task.id, "approvals"), what="a pending plan approval")
    response = fresh_client.post(f"/v1/tasks/{task.id}/cancel", headers=auth(user))
    assert response.status_code == 200
    thread.join(timeout=30)
    assert result == ["cancelled"]
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("cancelled", None)
    assert rig.envs[0].cleanups == 1
    assert status_events(fresh_db, task.id)[-1]["to"] == "cancelled"  # nothing written after


def test_stopping_a_runner_writes_nothing_and_still_cleans_up(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, steps=investigate_steps)
    runner = runner_for(rig, claim(fresh_db, rig.worker_id))
    assert runner.stopped is False
    thread, result = in_thread(runner.run)
    wait_until(lambda: _pending(fresh_db, task.id, "approvals"), what="a pending plan approval")
    runner.stop()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert runner.stopped is True
    assert status_of(fresh_db, task.id) == "awaiting_plan_approval"  # a stale worker stays quiet
    assert status_events(fresh_db, task.id)[-1]["to"] == "awaiting_plan_approval"
    assert rig.envs[0].cleanups == 1


# --- Worker: leases, heartbeat, lost leases --------------------------------------------------


def start_run_once(
    db: Database, tmp_path: Path, *, agent: dict[str, Any] | None = None, **kw: Any
) -> tuple[Rig, Worker, store.Task, threading.Thread, list[Any]]:
    task = queued_task(db)
    rig = make_rig(
        db, tmp_path, steps=investigate_steps, agent=agent or {"poll_seconds": 1.0}, **kw
    )
    worker = Worker(rig.deps, orphan_cleaner=Cleaner())
    thread, result = in_thread(worker.run_once)
    wait_until(lambda: status_of(db, task.id) == "awaiting_plan_approval", what="the plan wait")
    wait_until(lambda: bool(worker.running), what="the runner to be registered")
    return rig, worker, task, thread, result


def test_run_once_returns_none_when_nothing_is_queued(fresh_db: Database, tmp_path: Path) -> None:
    worker = Worker(make_rig(fresh_db, tmp_path).deps, orphan_cleaner=Cleaner())
    assert worker.run_once() is None
    assert worker.claim() is None


def test_heartbeat_renews_the_leases_of_running_tasks(fresh_db: Database, tmp_path: Path) -> None:
    rig, worker, task, thread, _ = start_run_once(fresh_db, tmp_path)
    before = task_row(fresh_db, task.id)["lease_expires_at"]
    time.sleep(0.3)
    assert worker.heartbeat() == []
    assert task_row(fresh_db, task.id)["lease_expires_at"] > before
    with fresh_db.backend() as conn:
        owner = task_owner(fresh_db, task.id)
        store.cancel_task(conn, owner, task.id)
    thread.join(timeout=30)
    assert not thread.is_alive()


def test_a_task_interrupted_elsewhere_is_a_lost_lease(fresh_db: Database, tmp_path: Path) -> None:
    rig, worker, task, thread, result = start_run_once(fresh_db, tmp_path)
    with fresh_db.backend() as conn:
        store.transition_task(conn, task.id, "interrupted", error="lease_expired")
    assert worker.heartbeat() == [task.id]
    thread.join(timeout=30)
    assert not thread.is_alive()
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("interrupted", "lease_expired")  # not overwritten
    assert status_events(fresh_db, task.id)[-1]["to"] == "interrupted"
    assert rig.envs[0].cleanups == 1


def test_a_lease_taken_over_by_another_worker_stops_the_runner_quietly(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig, worker, task, thread, _ = start_run_once(fresh_db, tmp_path)
    with fresh_db.backend() as conn:
        conn.execute("update public.tasks set worker_id = 'someone-else' where id = %s", [task.id])
    assert worker.heartbeat() == [task.id]
    thread.join(timeout=30)
    assert not thread.is_alive()
    row = task_row(fresh_db, task.id)
    assert row["status"] == "awaiting_plan_approval"
    assert row["worker_id"] == "someone-else"
    assert status_events(fresh_db, task.id)[-1]["to"] == "awaiting_plan_approval"
    assert rig.envs[0].cleanups == 1


def test_the_heartbeat_thread_keeps_renewing_during_a_long_wait(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(
        fresh_db,
        tmp_path,
        steps=investigate_steps,
        agent={"poll_seconds": 0.2},
        lease_seconds=3,
        heartbeat_seconds=1,
    )
    worker = Worker(rig.deps, orphan_cleaner=Cleaner())
    stop = threading.Event()
    server, _ = in_thread(lambda: worker.serve(stop))
    try:
        wait_until(lambda: status_of(fresh_db, task.id) == "awaiting_plan_approval")
        first = task_row(fresh_db, task.id)["heartbeat_at"]
        time.sleep(2.6)  # many wait polls and at least two heartbeat periods
        row = task_row(fresh_db, task.id)
        assert row["heartbeat_at"] > first
        assert status_of(fresh_db, task.id) == "awaiting_plan_approval"  # a 3 s lease did not lapse
        with fresh_db.backend() as conn:
            store.cancel_task(conn, task_owner(fresh_db, task.id), task.id)
        wait_until(lambda: not worker.running, what="the runner to finish")
    finally:
        stop.set()
        server.join(timeout=30)
    assert not server.is_alive()


# --- Worker.serve ---------------------------------------------------------------------------


def test_two_workers_run_four_tasks_each_exactly_once(fresh_db: Database, tmp_path: Path) -> None:
    ids = {queued_task(fresh_db, memory_mb=256, cpus=0.25).id for _ in range(4)}
    tracker = Tracker()
    rigs = [
        make_rig(
            fresh_db,
            tmp_path,
            worker_id=name,
            tracker=tracker,
            create_delay=0.3,
            steps=lambda: [[call("read_file", path="pager.py")]] * 2,
            agent={"step_limit": 1, "poll_seconds": 0.02},
        )
        for name in ("worker-a", "worker-b")
    ]
    workers = [Worker(r.deps, orphan_cleaner=Cleaner()) for r in rigs]
    stop = threading.Event()
    threads = [in_thread(lambda w=w: w.serve(stop))[0] for w in workers]
    try:
        for task_id in ids:
            wait_until(finished(fresh_db, task_id), timeout=90, what=f"task {task_id}")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    started = [r.task_id for rig in rigs for r in rig.requests]
    assert sorted(started) == sorted(str(i) for i in ids)  # each task started once, in one place
    assert tracker.peak <= 2  # the host limit holds across both workers
    assert tracker.active == 0
    for task_id in ids:
        assert task_row(fresh_db, task_id)["worker_id"] in {"worker-a", "worker-b"}


def test_serve_reconciles_leftovers_from_a_previous_run_first(
    fresh_db: Database, tmp_path: Path
) -> None:
    leftover = queued_task(fresh_db)
    set_active(fresh_db, leftover.id, worker_id="w1", lease=300)
    cleaner = Cleaner()
    rig = make_rig(fresh_db, tmp_path)
    worker = Worker(rig.deps, orphan_cleaner=cleaner)
    stop = threading.Event()
    server, _ = in_thread(lambda: worker.serve(stop))
    try:
        wait_until(finished(fresh_db, leftover.id), what="the leftover to be interrupted")
    finally:
        stop.set()
        server.join(timeout=30)
    assert task_row(fresh_db, leftover.id)["error"] == "worker_restarted"
    assert cleaner.calls
    assert cleaner.calls[0]["reap"] == {str(leftover.id)}
