"""Task worker: claim, prepare, run the agent, keep leases (m4-contract §7, §8, §12 A2).

Any number of workers may share one database. A worker claims the FIFO head under an advisory
lock (`store.claim_next_task`), holds a lease on each running task and renews it from a
separate heartbeat thread, and reaps tasks whose lease expired (another worker died). A runner
whose lease was lost stops without writing a status: the reaper's `interrupted` stands.

M5 (m5-contract §6): unless `RunnerDeps.evidence` opts out, each task gets a `TaskHarness`:
the baseline runs at the end of `preparing` (retryable), the start snapshot is taken before
`investigating`, the harness gates `complete_task`, and `confirming` re-runs the target.

M6 (m6-contract §7): `GitHubWorkspace` clones the repo in `preparing` (≤ max_retries, then
`clone_failed`); with a `RunnerDeps.publisher`, a confirmed task goes through `opening_pr`: the
evidence patch is applied to a fresh clone, pushed to `agent/<task-id>` and opened as a PR.

M8 (m8-contract §0.10, §0.12, §1.7): a task starts only with cost budget left, and the agent's
`cost_limit` is capped by it. A stop loop kills (`docker rm -f` + task network) the runners whose
task went terminal (admin kill, user cancel); a lost lease or a reap kills the same way.

Run: `uv run python -m swf.worker` (`SWF_LOCAL_SOURCE` = copy a local directory, no PR; dev).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
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
from swf.agent.web_agent import EvidenceGate, WebAgent, WebAgentConfig
from swf.config import AgentsFile, AgentsFileError, load_agents_file
from swf.db import Database
from swf.evidence import (
    HarnessInfraError,
    PgEvidenceSink,
    TaskHarness,
    UnsupportedChange,
    diff_snapshots,
    pr_patch,
    take_snapshot,
)
from swf.github import GitHubAuthError
from swf.gitops import GitOpsError, InvalidRepoName
from swf.guard import Guardrails
from swf.pr import (
    PrRequest,
    PrSummary,
    PublishError,
    PullRequestPublisher,
    TokenUnavailable,
    pr_title,
    render_pr_body,
)
from swf.preflight import Limits
from swf.settings import AbuseLimits, SettingsError

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
    "ReadOnlyViolation": "readonly_violation",
    "BaselineNotReproduced": "baseline_not_reproduced",
    "NoTestCommand": "no_test_command",
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
    base_sha: str | None = None  # set by GitHubWorkspace (the cloned HEAD)


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
    abuse: AbuseLimits = field(default_factory=AbuseLimits)
    stop_poll_seconds: float = 5.0  # m8 §0.12: kill switch / cancel -> container gone in 30 s

    def __post_init__(self) -> None:
        if not _WORKER_ID_RE.fullmatch(self.worker_id):
            raise ValueError(f"invalid worker id {self.worker_id!r}")
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds * 2 must be < lease_seconds")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if not 0 < self.stop_poll_seconds <= 10:
            raise ValueError("stop_poll_seconds must be > 0 and <= 10")


@dataclass
class RunnerDeps:
    db: Database
    workspaces: WorkspaceProvider
    env_factory: EnvFactory
    model_factory: Callable[[], AgentModel]
    config: WorkerConfig
    # None -> a `TaskHarness` per task (production); `AcceptAllEvidence()` opts out (M4).
    evidence: EvidenceGate | None = None
    # None -> no PR (M5: confirming -> completed); used only with a TaskHarness (m6 §0.11).
    publisher: PullRequestPublisher | None = None
    # None -> `Guardrails.off()` (no input guard, output guard on); m7 §5.
    guard_factory: Callable[[], Guardrails] | None = None
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


class _TaskFailed(Exception):
    """End the task as `failed(error)`."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


# The running task's channel, for publisher retry events (`GitHubPublisher.on_retry`).
_current_channel: contextvars.ContextVar[PgTaskChannel | None] = contextvars.ContextVar(
    "swf_current_channel", default=None
)


def emit_publish_retry(step: str, attempt: int, error: str) -> None:
    channel = _current_channel.get()
    if channel is not None:
        channel.emit("retry", {"step": step, "attempt": attempt, "error": error})


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
        self.harness: TaskHarness | None = None
        self.test_command: str | None = None
        self._env_lock = threading.Lock()  # kill() (another thread) vs the runner's cleanup
        self._env_released = False

    def stop(self) -> None:
        """Worker shutdown: stop at the next check, never write a status."""
        self.stopped = True

    def kill(self) -> None:
        """Kill switch / cancel / lost lease (m8 §0.12): stop, and remove the container now.

        Safe to call twice and from another thread. An in-flight `docker exec` then returns and
        the runner exits at its next status check, writing nothing.
        """
        self.stopped = True
        try:
            self._release_env()
        except Exception:
            logger.exception("killing task %s failed", self.task.id)

    def _release_env(self) -> None:
        """`env.cleanup()` (container + task network) once, whichever thread comes first."""
        with self._env_lock:
            env = self.env
            if env is not None and not self._env_released:
                env.cleanup()
                self._env_released = True  # rm errors are swallowed; orphan_cleaner backstops

    # --- run -------------------------------------------------------------------------------

    def run(self) -> str:
        """Run the task to an end state; returns the task's final status. Never raises."""
        try:
            self._run()
        except (states.TaskFinished, TaskStopped):
            pass
        except AgentExit as exc:
            self._fail(_EXIT_ERRORS.get(exc.status, _snake(exc.status)))
        except _TaskFailed as exc:
            self._fail(exc.error)
        except Exception as exc:
            logger.exception("task %s failed", self.task.id)
            self._fail(f"internal_error: {type(exc).__name__}")
        finally:
            self._cleanup()
        return self._final_status()

    def _run(self) -> None:
        deps, task, channel = self.deps, self.task, self.channel
        with deps.db.backend() as conn:  # m8 §0.10: no budget left -> no container
            budget = store.task_cost_budget(conn, task.id, deps.config.abuse)
        if budget <= 0:
            raise _TaskFailed("quota_cost_per_day")
        self.workspace = ws = self._prepare_workspace()
        if ws.base_sha:
            channel.emit(
                "repo_cloned",
                {"repo": task.repo, "base_branch": task.base_branch, "base_sha": ws.base_sha},
            )
        agents = self._agents_file(ws.repo)
        allow = list(agents.config.allow) if agents else []
        test_command = agents.config.test if agents else None
        self.test_command = test_command

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
        harness: TaskHarness | None = None
        if deps.evidence is None:
            harness = TaskHarness(
                env=env,
                channel=channel,
                sink=PgEvidenceSink(deps.db, task.id, is_stopped=lambda: self.stopped),
                repo=ws.repo,
                root=ws.root,
                test_command=test_command,
                failing_test=task.failing_test,
                output_limit=self.agent_config.output_limit,
                clock=deps.clock,
            )
            self.harness = harness
            self._with_retries("baseline", lambda attempt: harness.run_baseline())
            self._check_stopped()
            harness.take_start_snapshot()
        gate: EvidenceGate | None = harness if harness is not None else deps.evidence
        channel.transition("investigating", phase="investigate")
        agent_kwargs: dict[str, Any] = {
            "output_path": ws.root / "trajectory.json",
            "outputs_dir": ws.root / "outputs",
            "test_command": test_command,
            "allow": allow,
            "failing_test": task.failing_test,
            **deps.config.agent,
        }
        configured = float(agent_kwargs.get("cost_limit") or 0.0)
        agent_kwargs["cost_limit"] = budget if configured <= 0 else min(configured, budget)
        guard = deps.guard_factory() if deps.guard_factory else Guardrails.off()
        self.agent = WebAgent(
            deps.model_factory(),
            env,
            channel=channel,
            evidence=gate,
            guard=guard,
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
            if harness is None:
                channel.transition("completed")
                return
            self._confirm(harness)
            return
        self._fail(_EXIT_ERRORS.get(exit_status, _snake(exit_status) or "unknown_exit"))

    def _prepare_workspace(self) -> TaskWorkspace:
        """m6 §7: clone with ≤ max_retries attempts (GitOpsError only), then clone_failed."""
        cfg = self.agent_config
        attempts = max(1, cfg.max_retries)
        for attempt in range(1, attempts + 1):
            self._check_stopped()
            try:
                return self.deps.workspaces.prepare(self.task)
            except TokenUnavailable as exc:
                raise _TaskFailed(exc.code) from None
            except GitHubAuthError:
                raise _TaskFailed("github_token_invalid") from None
            except InvalidRepoName:
                raise _TaskFailed("clone_failed") from None
            except GitOpsError as exc:
                if exc.auth_failed:
                    raise _TaskFailed("github_token_invalid") from None
                self.channel.emit("retry", {"step": "clone", "attempt": attempt, "error": str(exc)})
                if attempt < attempts:
                    backoff = cfg.retry_backoff_seconds
                    if backoff:
                        self.deps.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
        raise _TaskFailed("clone_failed")

    def _summary(self, harness: TaskHarness, base_sha: str | None) -> PrSummary:
        digest = harness.checked_digest
        current = [r for r in harness.runs if r.workspace_digest == digest]
        post = next((r for r in reversed(current) if r.covers_target), None)
        suite = next((r for r in reversed(current) if r.selector is None), None)
        files = diff_snapshots(
            take_snapshot(harness.baseline_dir), take_snapshot(harness.repo)
        ).paths()
        return PrSummary(
            task_id=self.task.id,
            description=self.task.description,
            failing_test=self.task.failing_test,
            test_command=self.test_command,
            baseline=harness.baseline,
            baseline_suite=harness.baseline_suite,
            post=post,
            suite=suite,
            rerun=harness.rerun,
            files=files,
            base_sha=base_sha,
        )

    def _open_pr(self, harness: TaskHarness, publisher: PullRequestPublisher) -> None:
        """m6 §1: pr_patch -> publish -> record_pr -> completed."""
        task, ws = self.task, self.workspace
        assert ws is not None
        try:
            patch = pr_patch(harness.baseline_dir, ws.repo)
        except UnsupportedChange as exc:
            raise _TaskFailed(f"pr_failed: unsupported_change: {exc.path[:200]}") from None
        if not patch:
            raise _TaskFailed("pr_failed: empty_diff")
        req = PrRequest(
            task_id=task.id,
            repo=task.repo,
            base_branch=task.base_branch,
            base_sha=ws.base_sha,
            root=ws.root,
            patch=patch,
            title=pr_title(task),
            body=render_pr_body(self._summary(harness, ws.base_sha)),
        )
        self._check_stopped()
        ctx = _current_channel.set(self.channel)
        try:
            result = publisher.publish(req, task)
        except PublishError as exc:
            raise _TaskFailed(f"pr_failed: {exc.stage}") from None
        except TokenUnavailable as exc:
            raise _TaskFailed(exc.code) from None
        except GitHubAuthError:
            raise _TaskFailed("github_token_invalid") from None
        finally:
            _current_channel.reset(ctx)
        # The PR exists now: record it even if the lease was lost (like add_cost).
        with self.deps.db.backend() as conn:
            store.record_pr(
                conn,
                task.id,
                url=result.url,
                number=result.number,
                branch=result.branch,
                commit_sha=result.commit_sha,
            )
        self.channel.transition("completed")

    def _confirm(self, harness: TaskHarness) -> None:
        """m5 §0.5: the harness re-runs the target; infra failures retry, then fail."""
        cfg = self.agent_config
        attempts = max(1, cfg.max_retries)
        for attempt in range(1, attempts + 1):
            self._check_stopped()
            try:
                confirmation = harness.confirm()
            except HarnessInfraError as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.channel.emit(
                    "retry", {"step": "harness_rerun", "attempt": attempt, "error": error}
                )
                if attempt < attempts:
                    backoff = cfg.retry_backoff_seconds
                    if backoff:
                        self.deps.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                continue
            if confirmation.passed:
                publisher = self.deps.publisher
                if publisher is None:
                    self.channel.transition("completed")
                    return
                self.channel.transition("opening_pr", phase="pr")
                self._open_pr(harness, publisher)
                return
            reason = (confirmation.discrepancy or {}).get("reason", "unknown")
            self._fail(
                f"harness_mismatch: {reason}; post-fix exit 0, "
                f"harness re-run exit {confirmation.exit_code}"
            )
            return
        self._fail("harness_rerun_error")

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
                if self.stopped:  # killed: the failure is the removed container; no retry
                    raise TaskStopped() from exc
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
        try:
            self._release_env()
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
            runners = [self.running[t] for t in ids if t in self.running]
            active = [str(t) for t in self.running if t not in ids]
        for runner in runners:  # outside the lock: kill() waits for `docker rm -f`
            runner.kill()
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
            runners = [self.running[t] for t in lost if t in self.running]
        for runner in runners:
            runner.kill()
        return lost

    def enforce_stops(self) -> list[uuid.UUID]:
        """Kill the runners whose task went terminal (admin kill, user cancel; m8 §0.12)."""
        ids = self._running_ids()
        if not ids:
            return []
        with self.deps.db.backend() as conn:
            stopped = store.stopped_task_ids(conn, ids)
        with self._lock:
            runners = [self.running[t] for t in stopped if t in self.running]
        for runner in runners:
            runner.kill()
        return stopped

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

    def _stop_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.deps.config.stop_poll_seconds):
            try:
                self.enforce_stops()
            except Exception:
                logger.exception("stop loop failed")

    def _background(self, stop: threading.Event) -> list[threading.Thread]:
        """Start the heartbeat and stop-loop threads; both end when `stop` is set."""
        threads = [
            threading.Thread(target=self._heartbeat_loop, args=(stop,), daemon=True),
            threading.Thread(target=self._stop_loop, args=(stop,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        return threads

    def run_once(self) -> uuid.UUID | None:
        """Claim one task and run it synchronously, holding its lease (tests)."""
        task = self.claim()
        if task is None:
            return None
        runner = TaskRunner(self.deps, task)
        with self._lock:
            self.running[task.id] = runner
        stop = threading.Event()
        background = self._background(stop)
        try:
            runner.run()
        finally:
            stop.set()
            for thread in background:
                thread.join()
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
        background = self._background(beat_stop)
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
            for thread in background:
                thread.join()


# --- entry point ---------------------------------------------------------------------------


_AGENT_ENV = {
    "SWF_AGENT_STEP_LIMIT": "step_limit",
    "SWF_AGENT_WALL_TIME_SECONDS": "wall_time_limit_seconds",
}


def agent_overrides_from_env(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """Agent limit overrides from env (m9 §1.2); blank or missing keeps the product default."""
    env = os.environ if env is None else env
    out: dict[str, int] = {}
    for name, key in _AGENT_ENV.items():
        raw = env.get(name, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value <= 0:
            raise SettingsError(f"{name} must be a positive integer")
        out[key] = value
    return out


def main() -> None:
    config_dir = _mswea.isolate()
    assert os.environ["MSWEA_GLOBAL_CONFIG_DIR"] == str(config_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from swf.agent.model import PhaseModel
    from swf.egress import EgressConfig, ensure_egress
    from swf.github import GitHubClient
    from swf.gitops import GitOps
    from swf.pr import GitHubPublisher, GitHubWorkspace, db_token_source
    from swf.sandbox import (
        check_runtime,
        cleanup_orphans,
        open_task_sandbox,
        runtime_from_env,
        sandbox_user,
    )
    from swf.settings import Settings
    from swf.tokens import TokenCipher

    settings = Settings.from_env()
    Guardrails.from_env()  # fail fast: MissingApiKey exits before any task is claimed
    abuse = AbuseLimits.from_env()
    runtime = runtime_from_env()
    if runtime is not None:  # m8 §1.7: fail fast when docker lacks the runtime
        check_runtime(runtime)
    logger.info("sandbox runtime: %s", runtime or "docker default")
    source = os.environ.get("SWF_LOCAL_SOURCE")
    workspaces_root = Path(os.environ.get("SWF_WORKSPACES_ROOT") or "./workspaces").resolve()
    sandbox_user()  # m9 §0.7: fail fast on a root host without SWF_SANDBOX_USER
    agent = agent_overrides_from_env()
    egress = EgressConfig()
    ensure_egress(egress)
    config = WorkerConfig(
        worker_id=default_worker_id(), workspaces_root=workspaces_root, abuse=abuse, agent=agent
    )
    db = Database(settings.database_url, max_size=config.max_concurrent * 2 + 2)
    db.open()

    def env_factory(req: EnvRequest) -> ToolEnv:
        return open_task_sandbox(egress, req)

    workspaces: WorkspaceProvider
    publisher: PullRequestPublisher | None
    if source:  # dev: copy a local directory, no PR
        workspaces, publisher = LocalCopyWorkspace(Path(source), workspaces_root), None
    else:
        cipher = TokenCipher(settings.token_encryption_key)
        tokens = db_token_source(db, cipher)
        git = GitOps()
        api = GitHubClient(base_url=settings.github_api_url)
        workspaces = GitHubWorkspace(git, tokens, workspaces_root)
        publisher = GitHubPublisher(git, api, tokens, on_retry=emit_publish_retry)
    deps = RunnerDeps(
        db=db,
        workspaces=workspaces,
        env_factory=env_factory,
        model_factory=PhaseModel.from_env,
        config=config,
        publisher=publisher,
        guard_factory=Guardrails.from_env,
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
