"""`WebAgent`: mini-swe-agent's `DefaultAgent` with phases, policy, approvals and events
(m4-contract §5.4 and §12 A1).

Hooks over upstream: `query()` (stop check, limits on the injected clock, model retries, cost),
`execute_actions()` (per-tool dispatch through the phase policy and the command classifier),
`add_messages()` (a `message` event per message), `step()` (a concurrently finished task row
becomes `TaskStopped`). Upstream `run()` drives the loop and catches the exit exceptions.

Phases: investigate -> `submit_plan` -> (approved) execute -> edits -> a passing `run_tests`
that covers the target -> verify (switched by the harness) -> `complete_task`.
Waits (approvals, questions) poll the channel, are bounded by `approval_timeout_seconds`, and
do not count toward the wall-time limit.

M5 (m5-contract §4, §12 A1): the evidence gate defaults to `RefuseAllEvidence`. When the gate is
an `EvidenceHarness` (`swf.evidence.TaskHarness`), every non-infra `run_tests` result is handed
to `record_run`, each `submit_plan` first runs the read-only backstop (a change made in
investigate becomes a `readonly_violation` approval: rejected -> `ReadOnlyViolation`, approved
-> re-baseline), and `record_evidence` returns the harness's bundle status.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast, runtime_checkable

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, TimeExceeded
from pydantic import ValidationError

from swf import policy, states
from swf.agent import prompts
from swf.agent.channel import (
    ApprovalOutcome,
    ReadOnlyViolation,
    RetriesExhausted,
    SetupForbidden,
    TaskChannel,
    TaskStopped,
    TaskSubmitted,
    WaitSettings,
    WaitTimedOut,
    ask_and_wait,
    retry_question,
    wait_for,
)
from swf.agent.model import AgentModel
from swf.agent.tools import (
    CONTAINER_TOOLS,
    Plan,
    ToolEnv,
    ToolError,
    run_tool,
    validate_args,
)

__all__ = [
    "AcceptAllEvidence",
    "EvidenceGate",
    "EvidenceHarness",
    "ReadOnlyViolation",
    "RefuseAllEvidence",
    "RetriesExhausted",
    "SetupForbidden",
    "TaskStopped",
    "TaskSubmitted",
    "WaitTimedOut",
    "WebAgent",
    "WebAgentConfig",
]

T = TypeVar("T")
_ARGS_EVENT_LIMIT = 2000
_FILE_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@runtime_checkable
class EvidenceGate(Protocol):
    def check(self, channel: TaskChannel) -> str | None:
        """None = the task may complete; else the refusal text for the observation."""
        ...


@runtime_checkable
class EvidenceHarness(EvidenceGate, Protocol):
    """A gate that also records evidence and runs the backstop (m5 §4; `TaskHarness`)."""

    def check_readonly(self) -> dict[str, Any]:
        """`SnapshotDiff.as_payload()` of the workspace vs the start snapshot."""
        ...

    def record_run(
        self, *, tool_call_id: str, selector: str | None, output: dict[str, Any], phase: str
    ) -> None: ...

    def status_text(self) -> str: ...

    def rebaseline(self, approval_id: Any = None) -> None:
        """The user accepted an investigate-phase change: new baseline and start snapshot."""
        ...


class AcceptAllEvidence:
    """Explicit opt-out (M4 behaviour): every `complete_task` is accepted, no harness."""

    def check(self, channel: TaskChannel) -> str | None:
        return None


class RefuseAllEvidence:
    """Secure default when no gate is wired: completion is always refused (m5 §0.6)."""

    def check(self, channel: TaskChannel) -> str | None:
        return "complete_task refused: no evidence harness is configured"


class WebAgentConfig(AgentConfig):
    system_template: str = prompts.SYSTEM_TEMPLATE
    instance_template: str = prompts.INSTANCE_TEMPLATE
    step_limit: int = 150
    cost_limit: float = 0.0
    wall_time_limit_seconds: int = 3600
    approval_timeout_seconds: int = 86400
    poll_seconds: float = 2.0
    max_retries: int = 3
    retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0)
    output_limit: int = 16000
    test_command: str | None = None
    allow: list[str] = []
    outputs_dir: Path | None = None
    failing_test: str | None = None


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _truncate_args(args: Any, limit: int = _ARGS_EVENT_LIMIT) -> Any:
    if isinstance(args, str):
        return _truncate(args, limit)
    if isinstance(args, dict):
        return {k: _truncate_args(v, limit) for k, v in args.items()}
    if isinstance(args, list):
        return [_truncate_args(v, limit) for v in args]
    return args


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
        return "".join(str(p) for p in parts)
    return str(content)


def _infra_failure(out: dict[str, Any]) -> bool:
    return out.get("returncode") == -1 or bool(out.get("exception_info"))


def _result(output: str, returncode: int) -> dict[str, Any]:
    return {"output": output, "returncode": returncode, "exception_info": ""}


class WebAgent(DefaultAgent):
    def __init__(
        self,
        model: AgentModel,
        env: ToolEnv,
        *,
        channel: TaskChannel,
        evidence: EvidenceGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        config_class: type = WebAgentConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, env, config_class=config_class, **kwargs)  # type: ignore[arg-type]
        self.model: AgentModel = model
        self.env: ToolEnv = env  # type: ignore[assignment]
        self.channel = channel
        self.evidence: EvidenceGate = evidence if evidence is not None else RefuseAllEvidence()
        self.clock = clock
        self.sleep = sleep
        self.phase = "investigate"
        self.edits_since_approval = 0
        self._start_time = clock()

    @property
    def cfg(self) -> WebAgentConfig:
        return cast(WebAgentConfig, self.config)

    # --- upstream hooks --------------------------------------------------------------------

    def run(self, task: str = "", **kwargs: Any) -> dict[str, Any]:
        self.phase = "investigate"
        self.edits_since_approval = 0
        self.model.set_phase("investigate")
        self._start_time = self.clock()
        defaults: dict[str, Any] = {
            "repo": "",
            "failing_test": self.cfg.failing_test,
            "agents_prose": "",
            "test_command": self.cfg.test_command,
        }
        result: dict[str, Any] = super().run(task, **(defaults | kwargs))
        return result

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        merged: dict[str, Any] = super().get_template_vars(**kwargs)
        merged["elapsed_seconds"] = int(self.clock() - self._start_time)
        return merged

    def step(self) -> list[dict[str, Any]]:
        try:
            result: list[dict[str, Any]] = super().step()
        except states.TaskFinished as exc:
            raise TaskStopped() from exc
        return result

    def add_messages(self, *messages: dict[str, Any]) -> list[dict[str, Any]]:
        for message in messages:
            extra = message.get("extra") or {}
            payload: dict[str, Any] = {
                "role": message.get("role"),
                "content": _truncate(_content_text(message.get("content")), self.cfg.output_limit),
            }
            for key in ("model", "phase", "exit_status"):
                if key in extra:
                    payload[key] = extra[key]
            self.channel.emit("message", payload)
        added: list[dict[str, Any]] = super().add_messages(*messages)
        return added

    def query(self) -> dict[str, Any]:
        self._raise_if_stopped()
        cfg = self.cfg
        if 0 < cfg.step_limit <= self.n_calls or 0 < cfg.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < cfg.wall_time_limit_seconds <= int(self.clock() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )
        self.n_calls += 1
        try:
            message = self._with_retries("model", lambda: self.model.query(self.messages))
        except FormatError as exc:
            # Billed before parsing failed; upstream `run` adds it to `self.cost`.
            billed = float(exc.messages[0].get("extra", {}).get("cost", 0.0) or 0.0)
            if billed:
                self.channel.add_cost(billed)
            raise
        cost = float(message.get("extra", {}).get("cost", 0.0) or 0.0)
        self.cost += cost
        if cost:
            self.channel.add_cost(cost)
        self.add_messages(message)
        return message

    def execute_actions(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = message.get("extra", {}).get("actions", [])
        outputs: list[dict[str, Any]] = []
        for action in actions:
            output, stop = self._run_action(action)
            outputs.append(output)
            if stop:  # later actions are padded as "not executed" by upstream
                break
        observations: list[dict[str, Any]] = self.model.format_observation_messages(
            message, outputs, self.get_template_vars()
        )
        return self.add_messages(*observations)

    # --- helpers ---------------------------------------------------------------------------

    def _raise_if_stopped(self) -> None:
        if states.is_terminal(self.channel.status()):
            raise TaskStopped()

    def _transition(self, to_status: str, *, phase: str | None = None) -> None:
        try:
            self.channel.transition(to_status, phase=phase)
        except states.TaskFinished as exc:
            raise TaskStopped() from exc

    def _wait_settings(self) -> WaitSettings:
        return WaitSettings(
            timeout_seconds=self.cfg.approval_timeout_seconds,
            poll_seconds=self.cfg.poll_seconds,
            clock=self.clock,
            sleep=self.sleep,
        )

    def _timed_wait(self, fn: Callable[[], T]) -> T:
        """Run a wait; the time spent does not count toward the wall-time limit."""
        started = self.clock()
        try:
            return fn()
        finally:
            self._start_time += self.clock() - started

    def _wait_approval(self, approval_id: Any) -> ApprovalOutcome:
        outcome = self._timed_wait(
            lambda: wait_for(
                lambda: self.channel.approval(approval_id),
                channel=self.channel,
                timeout_seconds=self.cfg.approval_timeout_seconds,
                poll_seconds=self.cfg.poll_seconds,
                clock=self.clock,
                sleep=self.sleep,
                on_timeout=lambda: self.channel.expire_approval(approval_id),
                timeout_status="ApprovalTimeout",
            )
        )
        if outcome.status == "expired":
            self._raise_if_stopped()
            raise WaitTimedOut(exit_status="ApprovalTimeout")
        return outcome

    def _ask(self, question: str, source: Literal["agent", "system"]) -> str:
        back_to = states.PHASE_STATUS[self.phase]
        try:
            return self._timed_wait(
                lambda: ask_and_wait(
                    self.channel,
                    question,
                    source=source,
                    back_to=back_to,
                    wait=self._wait_settings(),
                )
            )
        except states.TaskFinished as exc:
            raise TaskStopped() from exc

    def _with_retries(self, step: str, fn: Callable[[], T]) -> T:
        """Up to `max_retries` attempts (backoff between), then a system question:
        `retry` -> another round, anything else -> `RetriesExhausted`."""
        cfg = self.cfg
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn()
            except InterruptAgentFlow:
                raise
            except (states.TaskFinished, ToolError):
                raise  # a refused call is not a retryable failure
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.channel.emit("retry", {"step": step, "attempt": attempt, "error": error})
                self._exhausted_or_wait(step, attempt, error)
                if attempt >= cfg.max_retries:
                    attempt = 0

    def _exhausted_or_wait(self, step: str, attempt: int, error: str) -> None:
        cfg = self.cfg
        if attempt < cfg.max_retries:
            backoff = cfg.retry_backoff_seconds
            if backoff:
                self.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
            self._raise_if_stopped()
            return
        answer = self._ask(retry_question(step, cfg.max_retries, error), "system")
        if answer.strip().lower() != "retry":
            raise RetriesExhausted()

    @property
    def harness(self) -> EvidenceHarness | None:
        return self.evidence if isinstance(self.evidence, EvidenceHarness) else None

    def _finish(
        self,
        tool: str,
        tool_call_id: str,
        out: dict[str, Any],
        suffix: str = "",
    ) -> dict[str, Any]:
        """Truncate for the model, keep the full output on disk, emit `tool_result`."""
        limit = self.cfg.output_limit
        full = str(out.get("output", "") or "")
        truncated = len(full) > limit
        shown = full[:limit]
        output_ref: str | None = None
        note = ""
        if truncated:
            if self.cfg.outputs_dir is not None:
                self.cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
                name = _FILE_SAFE.sub("_", tool_call_id or f"call-{self.n_calls}") + ".txt"
                path = self.cfg.outputs_dir / name
                path.write_text(full, encoding="utf-8", newline="\n")
                output_ref = str(path)
            note = f"\n[output truncated: showing {limit} of {len(full)} characters]"
        self.channel.emit(
            "tool_result",
            {
                "tool": tool,
                "tool_call_id": tool_call_id,
                "returncode": out.get("returncode"),
                "output": shown + suffix,
                "truncated": truncated,
                "output_ref": output_ref,
            },
        )
        return {**out, "output": shown + note + suffix}

    # --- dispatch --------------------------------------------------------------------------

    def _run_action(self, action: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        tool = str(action.get("tool") or ("bash" if "command" in action else ""))
        args: Any = action.get("args")
        if args is None:
            args = {"command": action["command"]} if "command" in action else {}
        tool_call_id = str(action.get("tool_call_id", ""))
        self.channel.emit(
            "tool_call",
            {"tool": tool, "args": _truncate_args(args), "tool_call_id": tool_call_id},
        )
        verdict = policy.check_tool(tool, self.phase)
        if verdict.decision == "forbid":
            self.channel.emit(
                "tool_refused",
                {"tool": tool, "tool_call_id": tool_call_id, "reasons": list(verdict.reasons)},
            )
            out = _result("forbidden: " + "; ".join(verdict.reasons), 126)
            return self._finish(tool, tool_call_id, out), False
        suffix = ""
        stop = False
        submitted: str | None = None
        try:
            validate_args(tool, args)
            if tool == "bash":
                out = self._bash(args)
            elif tool == "run_tests":
                out, stop = self._run_tests(action, args, tool_call_id)
                if stop:
                    suffix = prompts.VERIFY_SWITCH_NOTE
            elif tool in CONTAINER_TOOLS:
                out = run_tool(
                    self.env,
                    {"tool": tool, "args": args, "tool_call_id": tool_call_id},
                    test_command=self.cfg.test_command,
                    output_limit=self.cfg.output_limit,
                )
                if tool in ("edit_file", "write_file") and out.get("returncode") == 0:
                    self.edits_since_approval += 1
            elif tool == "submit_plan":
                out = self._submit_plan(args)
                stop = True
            elif tool == "ask_user":
                answer = self._ask(args["question"], "agent")
                out = _result(answer, 0)
            elif tool == "record_evidence":
                out = self._record_evidence(args)
            elif tool == "complete_task":
                refusal = self.evidence.check(self.channel)
                if refusal is None:
                    submitted = str(args["summary"])
                    out = _result("completed", 0)
                else:
                    out = _result(refusal, 1)
            else:  # pragma: no cover - check_tool refuses unknown tools
                raise ToolError(f"unknown tool {tool!r}")
        except ToolError as exc:
            out = _result(str(exc), 1)
        result = self._finish(tool, tool_call_id, out, suffix)
        if submitted is not None:
            self.channel.emit("completed", {"summary": submitted})
            raise TaskSubmitted(submitted)
        return result, stop

    def _bash(self, args: dict[str, Any]) -> dict[str, Any]:
        command: str = args["command"]
        verdict = policy.classify(command, phase=self.phase, allow=self.cfg.allow)
        self.channel.emit(
            "command_verdict",
            {"command": command, "decision": verdict.decision, "reasons": list(verdict.reasons)},
        )
        if verdict.decision == "forbid":
            return _result("forbidden: " + "; ".join(verdict.reasons), 126)
        if verdict.decision == "approve":
            # Status first: a pending approval row always implies the awaiting status.
            self._transition("awaiting_command_approval")
            approval_id = self.channel.request_approval(
                "command",
                {"command": command, "reasons": list(verdict.reasons), "phase": self.phase},
            )
            outcome = self._wait_approval(approval_id)
            self._transition(states.PHASE_STATUS[self.phase])
            if outcome.status == "rejected":
                return _result(f"rejected by user: {outcome.comment or ''}", 1)
        out: dict[str, Any] = self.env.execute(
            {"command": command}, timeout=args.get("timeout_seconds")
        )
        if out.get("returncode") == 0 and self._is_writer(command):
            self.edits_since_approval += 1
        return out

    def _is_writer(self, command: str) -> bool:
        """A command the read-only policy forbids, i.e. one that may modify the repo."""
        verdict = policy.classify(command, phase="investigate", allow=self.cfg.allow)
        return verdict.decision == "forbid"

    def _run_tests(
        self, action: dict[str, Any], args: dict[str, Any], tool_call_id: str
    ) -> tuple[dict[str, Any], bool]:
        def attempt() -> dict[str, Any]:
            out = run_tool(
                self.env,
                {"tool": "run_tests", "args": args, "tool_call_id": tool_call_id},
                test_command=self.cfg.test_command,
                output_limit=self.cfg.output_limit,
            )
            if _infra_failure(out):
                raise _InfraFailure(out)
            return out

        out = self._with_retries("run_tests", attempt)
        harness = self.harness
        if harness is not None:
            harness.record_run(
                tool_call_id=tool_call_id,
                selector=args.get("selector"),
                output=out,
                phase=self.phase,
            )
        if not self._switch_trigger(out, args.get("selector")):
            return out, False
        self._switch_to_verify(tool_call_id, args.get("selector"))
        return out, True

    def _switch_trigger(self, out: dict[str, Any], selector: str | None) -> bool:
        if self.phase != "execute":
            return False
        if out.get("returncode") != 0 or _infra_failure(out):
            return False
        if self.edits_since_approval < 1:
            return False
        target = self.cfg.failing_test
        return target is None or selector is None or target in selector

    def _switch_to_verify(self, tool_call_id: str, selector: str | None) -> None:
        edits = self.edits_since_approval
        self._transition("verifying", phase="verify")
        self.phase = "verify"
        self.model.set_phase("verify")
        self.channel.emit(
            "phase_switch",
            {
                "from": "execute",
                "to": "verify",
                "trigger": {
                    "tool_call_id": tool_call_id,
                    "selector": selector,
                    "edits_since_approval": edits,
                },
            },
        )
        self.channel.emit("phase_model", {"phase": "verify", "model": self._model_name()})

    def _model_name(self) -> str | None:
        name = getattr(self.model, "current_model", None)
        return None if name is None else str(name)

    def _record_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        note = {"kind": args["kind"], "note": args["note"], "command": args.get("command")}
        harness = self.harness
        if harness is None:  # opt-out (M4 stub)
            self.channel.emit("evidence", note)
            return _result("recorded", 0)
        # Untrusted agent text: an event, never an evidence row (m5 §0.7).
        self.channel.emit("evidence_note", note)
        return _result(harness.status_text(), 0)

    def _backstop(self, harness: EvidenceHarness) -> None:
        """m5 §12 A1: an investigate-phase change needs the user's approval to stand."""
        report = harness.check_readonly()
        if not any(report.get("counts", {}).values()):
            return
        self.channel.emit("backstop_violation", report)
        payload: dict[str, Any] = dict(report)
        readonly_diff = getattr(harness, "readonly_diff", None)
        if callable(readonly_diff):
            diff, truncated = readonly_diff()
            payload["diff"] = diff
            payload["diff_truncated"] = truncated
        self._transition("awaiting_command_approval")
        approval_id = self.channel.request_approval("readonly_violation", payload)
        outcome = self._wait_approval(approval_id)
        self._transition(states.PHASE_STATUS[self.phase])
        if outcome.status != "approved":
            raise ReadOnlyViolation()
        self._with_retries("baseline", lambda: harness.rebaseline(approval_id=approval_id))

    def _submit_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        harness = self.harness
        if harness is not None:
            self._backstop(harness)
        try:
            plan = Plan.model_validate(args)
        except ValidationError as exc:
            raise ToolError(f"invalid plan: {exc}") from exc
        self._transition("awaiting_plan_approval", phase="plan")
        approval_id = self.channel.request_approval("plan", plan.model_dump())
        outcome = self._wait_approval(approval_id)
        if outcome.status == "approved":
            self._transition("executing", phase="execute")
            self.phase = "execute"
            self.edits_since_approval = 0
            self.model.set_phase("execute")
            self.channel.emit("phase_model", {"phase": "execute", "model": self._model_name()})
            text = (
                "Plan approved. You are now in the execute phase: make the change, then run_tests."
            )
            if outcome.comment:
                text += f"\nUser comment: {outcome.comment}"
            return _result(text, 0)
        self._transition("investigating", phase="investigate")
        self.phase = "investigate"
        return _result(f"plan rejected by user: {outcome.comment or ''}", 1)


class _InfraFailure(Exception):
    """`run_tests` did not run (timeout, docker error): retryable, unlike a failing test."""

    def __init__(self, out: dict[str, Any]) -> None:
        self.out = out
        super().__init__(str(out.get("exception_info") or out.get("output") or "infra failure"))
