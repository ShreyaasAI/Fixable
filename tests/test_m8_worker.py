"""M8 worker: stop loop, kill switch, lost lease, pause and cost budget (m8-contract §1.7, §3 T9).

A `BlockingEnv` stands in for a container: `execute` blocks until `cleanup()` (what `docker rm -f`
does to an in-flight `docker exec`). Private database clone (`fresh_db`) throughout, because a
global kill would cancel other tests' tasks. Short poll intervals; nothing sleeps for long.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from m4_fakes import (
    SEED_REPO,
    TEST_COMMAND,
    AutoDecider,
    FakeEnv,
    ScriptedModel,
    happy_steps,
    make_db_user,
    queued_task,
    status_events,
    task_owner,
    task_row,
    wait_until,
)

from swf import policy, store
from swf.agent.web_agent import AcceptAllEvidence
from swf.db import Database
from swf.settings import AbuseLimits
from swf.worker import (
    EnvRequest,
    LocalCopyWorkspace,
    RunnerDeps,
    TaskRunner,
    Worker,
    WorkerConfig,
)

pytest_plugins = ["m4_fakes"]

SLEEP = "sleep 600"


class BlockingEnv(FakeEnv):
    """`execute` blocks until `cleanup()` runs; records when each happened."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.entered = threading.Event()
        self.released = threading.Event()
        self.cleanup_at: list[float] = []

    def execute(self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None) -> Any:
        self.executed.append(action["command"])
        self.entered.set()
        self.released.wait(timeout=60)  # backstop: a red run must not hang the suite
        return {"output": "", "returncode": -1, "exception_info": "container removed"}

    def cleanup(self) -> None:
        self.cleanup_at.append(time.monotonic())
        super().cleanup()
        self.released.set()


@dataclass
class Rig:
    deps: RunnerDeps
    envs: list[Any] = field(default_factory=list)
    requests: list[EnvRequest] = field(default_factory=list)


def write_source(tmp_path: Path, setup: list[str]) -> Path:
    source = tmp_path / "source"
    shutil.copytree(SEED_REPO, source)
    config = {
        "image": "python:3.12-slim",
        "setup": setup,
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
    blocking: bool = False,
    env_error: bool = False,
    setup: list[str] | None = None,
    steps: Any = None,
    **config: Any,
) -> Rig:
    rig = Rig(deps=None)  # type: ignore[arg-type]
    cfg = WorkerConfig(
        worker_id="w1",
        workspaces_root=tmp_path / "workspaces",
        poll_seconds=0.05,
        agent=config.pop("agent", {"poll_seconds": 0.02}),
        **config,
    )

    def env_factory(request: EnvRequest) -> Any:
        rig.requests.append(request)
        if env_error:  # a red run must fail fast instead of waiting for a plan approval
            raise RuntimeError("no container wanted")
        env: Any = BlockingEnv(request.workspace) if blocking else FakeEnv(request.workspace)
        rig.envs.append(env)
        return env

    rig.deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(write_source(tmp_path, setup or []), cfg.workspaces_root),
        env_factory=env_factory,
        model_factory=lambda: ScriptedModel(steps() if steps else []),
        config=cfg,
        evidence=AcceptAllEvidence(),
    )
    return rig


def status_of(db: Database, task_id: uuid.UUID) -> str:
    return str(task_row(db, task_id)["status"])


@contextmanager
def serving(rig: Rig) -> Iterator[tuple[Worker, threading.Thread]]:
    """`Worker.serve` in a thread; the blocking envs are released before it is stopped."""
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])
    stop = threading.Event()
    thread = threading.Thread(target=lambda: worker.serve(stop), daemon=True)
    thread.start()
    try:
        yield worker, thread
    finally:
        for env in rig.envs:
            if isinstance(env, BlockingEnv):
                env.released.set()
        stop.set()
        thread.join(timeout=30)
    assert not thread.is_alive()


FAST = {"stop_poll_seconds": 0.2, "heartbeat_seconds": 1, "lease_seconds": 5}


def start_blocked(db: Database, tmp_path: Path) -> tuple[Rig, store.Task]:
    """A queued task and a blocking-env rig whose setup is `sleep 600`."""
    assert policy.classify(SLEEP, phase="prepare").decision != "forbid"
    task = queued_task(db)
    rig = make_rig(db, tmp_path, blocking=True, setup=[SLEEP], **FAST)
    return rig, task


def wait_blocked(db: Database, rig: Rig, task: store.Task) -> BlockingEnv:
    wait_until(lambda: status_of(db, task.id) == "preparing", what="preparing")
    wait_until(lambda: rig.envs and rig.envs[0].entered.is_set(), what="the blocked execute")
    env: BlockingEnv = rig.envs[0]
    return env


def assert_stopped_quietly(db: Database, task_id: uuid.UUID, last: str, error: str | None) -> None:
    row = task_row(db, task_id)
    assert (row["status"], row["error"]) == (last, error)
    with db.backend() as conn:
        events = conn.execute(
            "select type, payload from public.events where task_id = %s order by seq", [task_id]
        ).fetchall()
    assert events[-1]["type"] == "status"  # nothing was written after the cancel
    assert events[-1]["payload"]["to"] == last
    assert status_events(db, task_id)[-1]["to"] == last


# --- 1. admin kill and user cancel stop the container ----------------------------------------


def test_admin_kill_removes_the_container_and_the_runner_exits_quietly(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig, task = start_blocked(fresh_db, tmp_path)
    admin = make_db_user(fresh_db)
    with serving(rig) as (worker, _):
        env = wait_blocked(fresh_db, rig, task)
        assert env.cleanup_at == []
        with fresh_db.backend() as conn:
            assert store.admin_kill(conn, admin.id, None) == [task.id]
        t_kill = time.monotonic()
        wait_until(lambda: env.cleanup_at, timeout=2, what="cleanup within 2 s")
        assert env.cleanup_at[0] - t_kill <= 2
        wait_until(lambda: not worker.running, timeout=5, what="the runner thread to end")
    assert_stopped_quietly(fresh_db, task.id, "cancelled", "admin_kill")


def test_user_cancel_removes_the_container_within_two_seconds(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig, task = start_blocked(fresh_db, tmp_path)
    with serving(rig) as (worker, _):
        env = wait_blocked(fresh_db, rig, task)
        with fresh_db.backend() as conn:
            store.cancel_task(conn, task_owner(fresh_db, task.id), task.id)
        t_cancel = time.monotonic()
        wait_until(lambda: env.cleanup_at, timeout=2, what="cleanup within 2 s")
        assert env.cleanup_at[0] - t_cancel <= 2
        wait_until(lambda: not worker.running, timeout=5, what="the runner thread to end")
    assert status_of(fresh_db, task.id) == "cancelled"
    assert status_events(fresh_db, task.id)[-1]["to"] == "cancelled"


# --- 2. lost lease ---------------------------------------------------------------------------


def test_a_lost_lease_kills_the_container_at_the_next_heartbeat(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig, task = start_blocked(fresh_db, tmp_path)
    with serving(rig):
        env = wait_blocked(fresh_db, rig, task)
        with fresh_db.backend() as conn:
            conn.execute("update public.tasks set worker_id = 'other' where id = %s", [task.id])
        # heartbeat_seconds = 1: the next heartbeat finds the lease gone. Kill, not just stop().
        wait_until(lambda: env.cleanup_at, timeout=4, what="cleanup after the lease was lost")
    row = task_row(fresh_db, task.id)
    assert (row["status"], row["worker_id"]) == ("preparing", "other")  # stale worker stays quiet


# --- 3. the pause stops new claims -----------------------------------------------------------


def test_paused_system_claims_nothing(fresh_db: Database, tmp_path: Path) -> None:
    admin = make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        assert store.admin_kill(conn, admin.id, None) == []
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, **FAST)
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])
    assert worker.run_once() is None
    assert worker.claim() is None
    assert status_of(fresh_db, task.id) == "queued"
    assert rig.requests == []


# --- 4. cost budget --------------------------------------------------------------------------


def set_day_cost(db: Database, task_id: uuid.UUID, usd: float) -> None:
    with db.backend() as conn:
        conn.execute(
            "insert into public.quotas_usage (user_id, day, cost_usd)"
            " select user_id, (created_at at time zone 'UTC')::date, %s from public.tasks"
            " where id = %s on conflict (user_id, day) do update set cost_usd = excluded.cost_usd",
            [usd, task_id],
        )


def test_no_budget_left_fails_the_task_before_any_container_starts(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    set_day_cost(fresh_db, task.id, 3.0)
    rig = make_rig(
        fresh_db,
        tmp_path,
        env_error=True,
        agent={"poll_seconds": 0.02, "retry_backoff_seconds": []},
        **FAST,
    )
    worker = Worker(rig.deps, orphan_cleaner=lambda *a, **k: [])

    with AutoDecider(fresh_db, answer="no"):  # red only: a started container's retry question
        assert worker.run_once() == task.id

    row = task_row(fresh_db, task.id)
    assert (row["status"], row["error"]) == ("failed", "quota_cost_per_day")
    assert rig.requests == []  # env_factory was never called


def run_to_completion(db: Database, rig: Rig, task: store.Task) -> TaskRunner:
    with db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=90)
    assert claimed is not None and claimed.id == task.id
    runner = TaskRunner(rig.deps, claimed)
    with AutoDecider(db):
        assert runner.run() == "completed"
    return runner


def scripted_finish() -> Any:
    return happy_steps()


def test_the_agent_cost_limit_is_the_remaining_daily_budget(
    fresh_db: Database, tmp_path: Path
) -> None:
    task = queued_task(fresh_db)
    set_day_cost(fresh_db, task.id, 2.5)  # day limit 3.0 -> 0.5 left; per task 1.0
    rig = make_rig(fresh_db, tmp_path, steps=scripted_finish, **FAST)

    runner = run_to_completion(fresh_db, rig, task)

    assert runner.agent is not None
    assert runner.agent.config.cost_limit == pytest.approx(0.5)


def test_a_smaller_configured_cost_limit_wins(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    set_day_cost(fresh_db, task.id, 2.5)
    rig = make_rig(
        fresh_db,
        tmp_path,
        steps=scripted_finish,
        agent={"poll_seconds": 0.02, "cost_limit": 0.2},
        **FAST,
    )

    runner = run_to_completion(fresh_db, rig, task)

    assert runner.agent is not None
    assert runner.agent.config.cost_limit == pytest.approx(0.2)


def test_the_default_budget_is_the_per_task_limit(fresh_db: Database, tmp_path: Path) -> None:
    task = queued_task(fresh_db)
    rig = make_rig(fresh_db, tmp_path, steps=scripted_finish, abuse=AbuseLimits(), **FAST)

    runner = run_to_completion(fresh_db, rig, task)

    assert runner.agent is not None
    assert runner.agent.config.cost_limit == pytest.approx(1.0)


# --- 5. config validation --------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0, -1, 11])
def test_stop_poll_seconds_must_be_in_range(tmp_path: Path, seconds: float) -> None:
    with pytest.raises(ValueError, match="stop_poll_seconds"):
        WorkerConfig(worker_id="w1", workspaces_root=tmp_path, stop_poll_seconds=seconds)


def test_stop_poll_seconds_bounds_are_accepted(tmp_path: Path) -> None:
    assert WorkerConfig(worker_id="w1", workspaces_root=tmp_path).stop_poll_seconds == 5.0
    assert WorkerConfig(worker_id="w1", workspaces_root=tmp_path, stop_poll_seconds=10)
