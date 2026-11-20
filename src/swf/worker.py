"""Task worker: claim, prepare, run the agent, keep leases (m4-contract §7, §8, §12 A2).

Any number of workers may share one database. A worker claims the FIFO head under an advisory
lock (`store.claim_next_task`), holds a lease on each running task and renews it from a
separate heartbeat thread, and reaps tasks whose lease expired (another worker died). A runner
whose lease was lost stops without writing a status: the reaper's `interrupted` stands.

Run: `uv run python -m swf.worker` (needs `SWF_LOCAL_SOURCE` until M6 adds the GitHub clone).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol

from swf import _mswea, policy, states, store
from swf.agent.channel import (
    AgentExit,
    PgTaskChannel,
    RetriesExhausted,
    SetupForbidden,
    TaskStopped,
    WaitSettings,
    ask_and_wait,
    retry_question,
)
from swf.agent.model import AgentModel
from swf.agent.tools import ToolEnv
from swf.agent.web_agent import AcceptAllEvidence, EvidenceGate, WebAgent, WebAgentConfig
from swf.config import AgentsFile, AgentsFileError, load_agents_file
from swf.db import Database
from swf.preflight import Limits

logger = logging.getLogger("swf.worker")

_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SETUP_TIMEOUT_SECONDS = 600
_EXIT_ERRORS = {
    "ApprovalTimeout": "approval_timeout",
    "QuestionTimeout": "question_timeout",
    "LimitsExceeded": "limits_exceeded",
    "TimeExceeded": "time_exceeded",
    "RepeatedFormatError": "repeated_format_error",
    "RetriesExhausted": "retries_exhausted",
    "SetupForbidden": "setup_forbidden",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --- workspace and environment -------------------------------------------------------------


@dataclass(frozen=True)
class EnvRequest:
    task_id: str
    image: str
    workspace: Path
    memory_mb: int
    cpus: float
    worker_id: str | None = None


EnvFactory = Callable[[EnvRequest], ToolEnv]


@dataclass(frozen=True)
class TaskWorkspace:
    root: Path  # workspaces/<task-id>
    repo: Path  # root/repo (mounted at /repo)


class WorkspaceProvider(Protocol):
    def prepare(self, task: store.Task) -> TaskWorkspace: ...


class LocalCopyWorkspace:
    """Copies a local directory as the task's repo (M4 smoke test; M6 clones from GitHub)."""

    def __init__(self, source: Path, workspaces_root: Path) -> None:
        self.source = Path(source)
        self.workspaces_root = Path(workspaces_root)

    def prepare(self, task: store.Task) -> TaskWorkspace:
        root = (self.workspaces_root / str(task.id)).resolve()
        repo = root / "repo"
        if repo.exists():
            shutil.rmtree(repo)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.source, repo)
        return TaskWorkspace(root=root, repo=repo)


# --- configuration -------------------------------------------------------------------------


def default_worker_id(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return source.get("SWF_WORKER_ID") or socket.gethostname()


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    workspaces_root: Path
    max_concurrent: int = 2
    poll_seconds: float = 2.0
    limits: Limits = field(default_factory=Limits)
    agent: dict[str, Any] = field(default_factory=dict)
    lease_seconds: int = 90
    heartbeat_seconds: int = 30

    def __post_init__(self) -> None:
        if not _WORKER_ID_RE.fullmatch(self.worker_id):
            raise ValueError(f"invalid worker id {self.worker_id!r}")
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds * 2 must be < lease_seconds")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")


@dataclass
class RunnerDeps:
    db: Database
    workspaces: WorkspaceProvider
    env_factory: EnvFactory
    model_factory: Callable[[], AgentModel]
    config: WorkerConfig
    evidence: EvidenceGate = field(default_factory=AcceptAllEvidence)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


# --- one task ------------------------------------------------------------------------------


class _GuardedChannel(PgTaskChannel):
    """A stopped runner (lost lease) reads as terminal and writes nothing."""

    def __init__(self, db: Database, task_id: uuid.UUID, is_stopped: Callable[[], bool]) -> None:
        super().__init__(db, task_id)
        self._is_stopped = is_stopped

    def _guard(self) -> None:
        if self._is_stopped():
            raise states.TaskFinished("runner stopped")

    def status(self) -> str:
        return "interrupted" if self._is_stopped() else super().status()

    def transition(
        self, to_status: str, *, phase: str | None = None, error: str | None = None
    ) -> None:
        self._guard()
        super().transition(to_status, phase=phase, error=error)

    def emit(self, type: str, payload: dict[str, Any]) -> None:
        if not self._is_stopped():
            super().emit(type, payload)

    def request_approval(self, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        self._guard()
        return super().request_approval(kind, payload)

    def ask(self, question: str, *, source: Literal["agent", "system"]) -> uuid.UUID:
        self._guard()
        return super().ask(question, source=source)

    def add_cost(self, usd: float) -> None:
        # Spend happened even if the lease was lost; record it.
        super().add_cost(usd)


class _StepFailed(Exception):
    pass


class TaskRunner:
    def __init__(self, deps: RunnerDeps, task: store.Task) -> None:
        self.deps = deps
        self.task = task
        self.stopped = False
        self.channel = _GuardedChannel(deps.db, task.id, lambda: self.stopped)
        self.agent_config = WebAgentConfig(**deps.config.agent)
        self.env: ToolEnv | None = None
        self.agent: WebAgent | None = None
        self.workspace: TaskWorkspace | None = None

    def stop(self) -> None:
        """Lost lease or worker shutdown: stop at the next check, never write a status."""
        self.stopped = True

    # --- run -------------------------------------------------------------------------------

    def run(self) -> str:
        """Run the task to an end state; returns the task's final status. Never raises."""
        try:
            self._run()
        except (states.TaskFinished, TaskStopped):
            pass
        except AgentExit as exc:
            self._fail(_EXIT_ERRORS.get(exc.status, _snake(exc.status)))
        except Exception as exc:
            logger.exception("task %s failed", self.task.id)
            self._fail(f"internal_error: {type(exc).__name__}")
        finally:
            self._cleanup()
        return self._final_status()

    def _run(self) -> None:
        deps, task, channel = self.deps, self.task, self.channel
        self.workspace = ws = deps.workspaces.prepare(task)
        agents = self._agents_file(ws.repo)
        allow = list(agents.config.allow) if agents else []
        test_command = agents.config.test if agents else None

        preflight = task.preflight
        limits = deps.config.limits
        request = EnvRequest(
            task_id=str(task.id),
            image=preflight.image if preflight else limits.default_image,
            workspace=ws.repo,
            memory_mb=task.reservation_mb or (preflight.memory_mb if preflight else 4096),
            cpus=task.reservation_cpu or (preflight.cpus if preflight else 1.0),
            worker_id=deps.config.worker_id,
        )
        self.env = self._with_retries("container", lambda attempt: deps.env_factory(request))
        env = self.env

        for command in agents.config.setup if agents else []:
            verdict = policy.classify(command, phase="prepare", allow=allow)
            if verdict.decision == "forbid":
                channel.emit(
                    "setup",
                    {"command": command, "attempt": 0, "returncode": None, "forbidden": True},
                )
                raise SetupForbidden()

            def run_setup(attempt: int, command: str = command) -> None:
                out = env.execute({"command": command}, timeout=SETUP_TIMEOUT_SECONDS)
                rc = out.get("returncode")
                channel.emit("setup", {"command": command, "attempt": attempt, "returncode": rc})
                if rc != 0:
                    tail = str(out.get("output") or out.get("exception_info") or "")[-500:]
                    raise _StepFailed(f"setup command exited {rc}: {tail}")

            self._with_retries("setup", run_setup)

        self._check_stopped()
        channel.transition("investigating", phase="investigate")
        agent_kwargs: dict[str, Any] = {
            "output_path": ws.root / "trajectory.json",
            "outputs_dir": ws.root / "outputs",
            "test_command": test_command,
            "allow": allow,
            "failing_test": task.failing_test,
            **deps.config.agent,
        }
        self.agent = WebAgent(
            deps.model_factory(),
            env,
            channel=channel,
            evidence=deps.evidence,
            clock=deps.clock,
            sleep=deps.sleep,
            **agent_kwargs,
        )
        result = self.agent.run(
            task.description,
            repo=task.repo,
            failing_test=task.failing_test,
            agents_prose=agents.prose if agents else "",
            test_command=test_command,
        )
        exit_status = str(result.get("exit_status", ""))
        if self.stopped or exit_status == "Stopped":
            return
        if exit_status == "Submitted":
            channel.transition("confirming", phase="confirm")
            channel.transition("completed")
            return
        self._fail(_EXIT_ERRORS.get(exit_status, _snake(exit_status) or "unknown_exit"))

    @staticmethod
    def _agents_file(repo: Path) -> AgentsFile | None:
        try:
            return load_agents_file(repo)
        except (AgentsFileError, OSError, UnicodeDecodeError):
            return None

    def _check_stopped(self) -> None:
        if states.is_terminal(self.channel.status()):
            raise TaskStopped()

    def _with_retries(self, step: str, fn: Callable[[int], Any]) -> Any:
        """§0.2: ≤ max_retries attempts, then a system question (`retry` -> another round)."""
        cfg = self.agent_config
        attempt = 0
        while True:
            self._check_stopped()
            attempt += 1
            try:
                return fn(attempt)
            except (states.TaskFinished, AgentExit):
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.channel.emit("retry", {"step": step, "attempt": attempt, "error": error})
                if attempt < cfg.max_retries:
                    backoff = cfg.retry_backoff_seconds
                    if backoff:
                        self.deps.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                    continue
                answer = ask_and_wait(
                    self.channel,
                    retry_question(step, cfg.max_retries, error),
                    source="system",
                    back_to="preparing",
                    wait=WaitSettings(
                        timeout_seconds=cfg.approval_timeout_seconds,
                        poll_seconds=cfg.poll_seconds,
                        clock=self.deps.clock,
                        sleep=self.deps.sleep,
                    ),
                )
                if answer.strip().lower() != "retry":
                    raise RetriesExhausted() from exc
                attempt = 0

    def _fail(self, error: str) -> None:
        if self.stopped:
            return
        with contextlib.suppress(states.TaskFinished, states.InvalidTransition):
            self.channel.transition("failed", error=error[:10000])

    def _cleanup(self) -> None:
        if self.env is not None:
            try:
                self.env.cleanup()
            except Exception:
                logger.exception("cleanup of task %s failed", self.task.id)
        if self.agent is not None and self.workspace is not None:
            try:
                self.agent.save(self.workspace.root / "trajectory.json")
            except Exception:
                logger.exception("saving the trajectory of task %s failed", self.task.id)

    def _final_status(self) -> str:
        try:
            with self.deps.db.backend() as conn:
                status = store.get_task_status(conn, self.task.id)
        except Exception:
            logger.exception("reading the status of task %s failed", self.task.id)
            return "unknown"
        return status or "unknown"


# --- the worker ----------------------------------------------------------------------------


class Worker:
    def __init__(self, deps: RunnerDeps, *, orphan_cleaner: Callable[..., list[str]]) -> None:
        self.deps = deps
        self.id = deps.config.worker_id
        self.orphan_cleaner = orphan_cleaner
        self.running: dict[uuid.UUID, TaskRunner] = {}
        self._threads: dict[uuid.UUID, threading.Thread] = {}
        self._lock = threading.Lock()

    def _running_ids(self) -> list[uuid.UUID]:
        with self._lock:
            return list(self.running)

    def reconcile(self, *, restart: bool) -> list[uuid.UUID]:
        """Interrupt expired-lease tasks (and, on restart, our own), then remove leftovers."""
        cfg = self.deps.config
        with self.deps.db.backend() as conn:
            reaped = store.interrupt_orphans(conn, worker_id=cfg.worker_id, restart=restart)
        ids = [task_id for task_id, _ in reaped]
        with self._lock:
            for task_id in ids:
                runner = self.running.get(task_id)
                if runner is not None:
                    runner.stop()
            active = [str(t) for t in self.running if t not in ids]
        self.orphan_cleaner(active, worker_id=cfg.worker_id, reap_task_ids=[str(t) for t in ids])
        return ids

    def heartbeat(self) -> list[uuid.UUID]:
        """Renew the leases of running tasks; stop the runners whose lease was lost."""
        ids = self._running_ids()
        if not ids:
            return []
        cfg = self.deps.config
        with self.deps.db.backend() as conn:
            kept = set(
                store.renew_leases(
                    conn, worker_id=cfg.worker_id, task_ids=ids, lease_seconds=cfg.lease_seconds
                )
            )
        lost = [t for t in ids if t not in kept]
        with self._lock:
            for task_id in lost:
                runner = self.running.get(task_id)
                if runner is not None:
                    runner.stop()
        return lost

    def claim(self) -> store.Task | None:
        cfg = self.deps.config
        with self.deps.db.backend() as conn:
            return store.claim_next_task(
                conn, worker_id=cfg.worker_id, lease_seconds=cfg.lease_seconds, limits=cfg.limits
            )

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.deps.config.heartbeat_seconds):
            try:
                self.heartbeat()
            except Exception:
                logger.exception("heartbeat failed")

    def run_once(self) -> uuid.UUID | None:
        """Claim one task and run it synchronously, holding its lease (tests)."""
        task = self.claim()
        if task is None:
            return None
        runner = TaskRunner(self.deps, task)
        with self._lock:
            self.running[task.id] = runner
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(stop,), daemon=True)
        beat.start()
        try:
            runner.run()
        finally:
            stop.set()
            beat.join()
            with self._lock:
                self.running.pop(task.id, None)
        return task.id

    def _start(self, task: store.Task) -> None:
        runner = TaskRunner(self.deps, task)

        def target() -> None:
            try:
                runner.run()
            finally:
                with self._lock:
                    self.running.pop(task.id, None)

        thread = threading.Thread(target=target, name=f"swf-task-{task.id}", daemon=True)
        with self._lock:
            self.running[task.id] = runner
            self._threads[task.id] = thread
        thread.start()

    def _reap_threads(self) -> None:
        with self._lock:
            done = [t for t, th in self._threads.items() if not th.is_alive()]
            for task_id in done:
                del self._threads[task_id]

    def serve(self, stop: threading.Event) -> None:
        cfg = self.deps.config
        self.reconcile(restart=True)
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(beat_stop,), daemon=True)
        beat.start()
        last_reconcile = time.monotonic()
        try:
            while not stop.is_set():
                self._reap_threads()
                if time.monotonic() - last_reconcile >= cfg.heartbeat_seconds:
                    last_reconcile = time.monotonic()
                    try:
                        self.reconcile(restart=False)
                    except Exception:
                        logger.exception("reconcile failed")
                while len(self._running_ids()) < cfg.max_concurrent and not stop.is_set():
                    try:
                        task = self.claim()
                    except Exception:
                        logger.exception("claim failed")
                        task = None
                    if task is None:
                        break
                    self._start(task)
                stop.wait(cfg.poll_seconds)
        finally:
            with self._lock:
                runners = list(self.running.values())
                threads = list(self._threads.values())
            for runner in runners:
                runner.stop()
            for thread in threads:
                thread.join()
            beat_stop.set()
            beat.join()


# --- entry point ---------------------------------------------------------------------------


def main() -> None:
    config_dir = _mswea.isolate()
    assert os.environ["MSWEA_GLOBAL_CONFIG_DIR"] == str(config_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from swf.agent.model import PhaseModel
    from swf.egress import EgressConfig, ensure_egress
    from swf.sandbox import cleanup_orphans, open_task_sandbox
    from swf.settings import Settings

    settings = Settings.from_env()
    source = os.environ.get("SWF_LOCAL_SOURCE")
    if not source:
        raise SystemExit(
            "SWF_LOCAL_SOURCE is not set: M4 has no GitHub clone (M6), so the worker needs a "
            "local repository directory to copy for each task"
        )
    workspaces_root = Path(os.environ.get("SWF_WORKSPACES_ROOT") or "./workspaces").resolve()
    egress = EgressConfig()
    ensure_egress(egress)
    config = WorkerConfig(worker_id=default_worker_id(), workspaces_root=workspaces_root)
    db = Database(settings.database_url, max_size=config.max_concurrent * 2 + 2)
    db.open()

    def env_factory(req: EnvRequest) -> ToolEnv:
        return open_task_sandbox(egress, req)

    deps = RunnerDeps(
        db=db,
        workspaces=LocalCopyWorkspace(Path(source), workspaces_root),
        env_factory=env_factory,
        model_factory=PhaseModel.from_env,
        config=config,
    )
    worker = Worker(deps, orphan_cleaner=partial(cleanup_orphans, egress=egress))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, lambda *_: stop.set())
    try:
        worker.serve(stop)
    finally:
        db.close()


if __name__ == "__main__":
    main()
