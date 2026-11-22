"""Fakes and builders for the M4 tests (m4-contract §10).

Nothing here sleeps for real: `FakeClock.sleep` only advances a counter. `FakeChannel`,
`ScriptedModel` and `FakeEnv` implement the public protocols (`TaskChannel`, `AgentModel`,
`ToolEnv`) so the agent is tested through its public interface only.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm
import pytest
from helpers import FakeUser
from minisweagent.models.litellm_model import LitellmModelConfig
from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages

from swf import states, store
from swf.agent.channel import ApprovalOutcome
from swf.agent.tools import ToolError
from swf.agent.web_agent import AcceptAllEvidence, WebAgent
from swf.db import Database
from swf.preflight import PreflightResult

ROOT = Path(__file__).resolve().parent.parent
SEED_REPO = ROOT / "seeded_repos" / "01-paginate-off-by-one" / "repo"
FAILING_TEST = "tests/test_pager.py::test_second_page"
TEST_COMMAND = "python -m pytest -q"
OBSERVATION_TEMPLATE: str = LitellmModelConfig.model_fields["observation_template"].default

# --- clock ---------------------------------------------------------------------------------


class FakeClock:
    """Monotonic clock that only moves when `sleep` is called."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.hooks: list[Callable[[FakeClock], None]] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        for hook in list(self.hooks):
            hook(self)


# --- actions and the scripted model --------------------------------------------------------

_counter = 0


def call(tool: str, id: str | None = None, **args: Any) -> dict[str, Any]:  # noqa: A002
    """One action in the shape `PhaseModel` produces (contract §5.1)."""
    global _counter
    _counter += 1
    action: dict[str, Any] = {"tool": tool, "args": args, "tool_call_id": id or f"call_{_counter}"}
    if tool == "bash":
        action["command"] = args["command"]
    return action


class ScriptedModel:
    """`AgentModel` that replays `steps` (a list of actions, or an Exception to raise)."""

    def __init__(
        self, steps: Sequence[list[dict[str, Any]] | Exception], *, cost: float = 0.0
    ) -> None:
        self.steps = list(steps)
        self.cost = cost
        self.config = SimpleNamespace(model_name="scripted")
        self.phase = "investigate"
        self.phases: list[str] = []
        self.set_phase_calls: list[str] = []
        self.queries = 0
        self.before_query: Callable[[int], None] | None = None

    def set_phase(self, phase: str) -> None:
        self.set_phase_calls.append(phase)
        self.phase = phase

    @property
    def current_model(self) -> str:
        return f"fake-{self.phase}"

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.queries += 1
        if self.before_query is not None:
            self.before_query(self.queries)
        self.phases.append(self.phase)
        if not self.steps:
            raise AssertionError("ScriptedModel ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": a["tool_call_id"],
                    "type": "function",
                    "function": {"name": a["tool"], "arguments": json.dumps(a["args"])},
                }
                for a in step
            ],
            "extra": {
                "actions": step,
                "cost": self.cost,
                "model": f"fake-{self.phase}",
                "phase": self.phase,
            },
        }

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def format_observation_messages(
        self, message: dict[str, Any], outputs: list[dict[str, Any]], template_vars: Any = None
    ) -> list[dict[str, Any]]:
        return format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=OBSERVATION_TEMPLATE,
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {"model_name": "scripted"}

    def serialize(self) -> dict[str, Any]:
        return {"info": {"config": {"model": {"model_name": "scripted"}}}}


# --- litellm.completion stand-in -----------------------------------------------------------


def tool_response(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    id: str = "call_1",  # noqa: A002
    raw_arguments: str | None = None,
) -> Any:
    return tool_calls_response([(name, args or {}, id)], raw_arguments=raw_arguments)


def tool_calls_response(
    calls: Sequence[tuple[str, dict[str, Any], str]], *, raw_arguments: str | None = None
) -> Any:
    return litellm.ModelResponse(
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": raw_arguments
                                if raw_arguments is not None
                                else json.dumps(args),
                            },
                        }
                        for name, args, cid in calls
                    ],
                },
            }
        ],
        model="fake",
    )


def text_response(content: str) -> Any:
    return litellm.ModelResponse(
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        model="fake",
    )


class FakeCompletion:
    """Records every call's kwargs; replays `responses` (the last one repeats)."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


# --- environment ---------------------------------------------------------------------------

BashScript = dict[str, tuple[str, int]] | Callable[[str], dict[str, Any]]


class FakeEnv:
    """`ToolEnv` over a host directory (`/repo` <-> `root`)."""

    def __init__(self, root: Path, bash: BashScript | None = None) -> None:
        self.root = root
        self._bash: BashScript = bash if bash is not None else {}
        self.executed: list[str] = []
        self.timeouts: list[int | None] = []
        self.cleanups = 0
        self.config = SimpleNamespace()
        self._failures: list[str] = []

    def fail_next(self, n: int, kind: str = "infra") -> None:
        """The next `n` executes fail: kind "infra" (rc -1 + exception_info) or "raise"."""
        self._failures += [kind] * n

    def execute(
        self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        command = action["command"]
        self.executed.append(command)
        self.timeouts.append(timeout)
        if self._failures:
            if self._failures.pop(0) == "raise":
                raise RuntimeError("env exploded")
            return {"output": "", "returncode": -1, "exception_info": "infra failure: timed out"}
        if callable(self._bash):
            return self._bash(command)
        output, returncode = self._bash.get(command, ("", 0))
        return {"output": output, "returncode": returncode, "exception_info": ""}

    def _host(self, path: str) -> Path:
        assert path == "/repo" or path.startswith("/repo/"), f"unresolved path {path!r}"
        return self.root / path[len("/repo") :].lstrip("/")

    def read_bytes(self, path: str) -> bytes:
        target = self._host(path)
        if not target.is_file():
            raise ToolError(f"no such file: {path}")
        return target.read_bytes()

    def write_bytes(self, path: str, data: bytes) -> None:
        target = self._host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def list_dir(self, path: str) -> list[str]:
        target = self._host(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        return [c.name + "/" if c.is_dir() else c.name for c in sorted(target.iterdir())]

    def search(self, pattern: str, path: str, glob: str | None, max_results: int) -> list[str]:
        regex = re.compile(pattern)
        base = self._host(path)
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        found: list[str] = []
        for file in files:
            if glob and not fnmatch.fnmatch(file.name, glob):
                continue
            virtual = "/repo/" + file.relative_to(self.root).as_posix()
            text = file.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    found.append(f"{virtual}:{number}:{line}")
        return found[:max_results]

    def cleanup(self) -> None:
        self.cleanups += 1

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def serialize(self) -> dict[str, Any]:
        return {"info": {"config": {"environment": {}, "environment_type": "FakeEnv"}}}


# --- channel -------------------------------------------------------------------------------


def auto_approve(kind: str, payload: dict[str, Any]) -> ApprovalOutcome | None:
    return ApprovalOutcome("approved", None)


def no_answer(question: str) -> str | None:
    return None


class FakeChannel:
    """In-memory `TaskChannel`; transitions are validated with `swf.states`."""

    def __init__(
        self,
        status: str = "investigating",
        *,
        decide: Callable[[str, dict[str, Any]], ApprovalOutcome | None] = auto_approve,
        answers: Callable[[str], str | None] = no_answer,
    ) -> None:
        self.task_id = uuid.uuid4()
        self._status = status
        self.decide = decide
        self.answers = answers
        self.cancelled = False
        self.transitions: list[tuple[str, str | None]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.costs: list[float] = []
        self.approvals: dict[uuid.UUID, tuple[str, dict[str, Any]]] = {}
        self._outcomes: dict[uuid.UUID, ApprovalOutcome] = {}
        self.expired_approvals: list[uuid.UUID] = []
        self.asked: list[tuple[str, str]] = []  # (question, source)
        self._questions: dict[uuid.UUID, str] = {}
        self._answered: dict[uuid.UUID, str] = {}
        self.expired_questions: list[uuid.UUID] = []

    def cancel(self) -> None:
        self.cancelled = True

    def force_status(self, status: str) -> None:
        """Test-only: move the status without validating (to stage unreachable situations)."""
        self._status = status

    def status(self) -> str:
        return "cancelled" if self.cancelled else self._status

    def transition(
        self, to_status: str, *, phase: str | None = None, error: str | None = None
    ) -> None:
        current = self.status()
        states.check_transition(current, to_status)
        self._status = to_status
        self.transitions.append((to_status, phase))
        self.events.append(
            ("status", {"from": current, "to": to_status, "phase": phase, "error": error})
        )

    def emit(self, type: str, payload: dict[str, Any]) -> None:  # noqa: A002
        self.events.append((type, payload))

    def request_approval(self, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        approval_id = uuid.uuid4()
        self.approvals[approval_id] = (kind, payload)
        return approval_id

    def approval(self, approval_id: uuid.UUID) -> ApprovalOutcome | None:
        if approval_id in self._outcomes:
            return self._outcomes[approval_id]
        kind, payload = self.approvals[approval_id]
        outcome = self.decide(kind, payload)
        if outcome is not None:
            self._outcomes[approval_id] = outcome
        return outcome

    def expire_approval(self, approval_id: uuid.UUID) -> None:
        self.expired_approvals.append(approval_id)

    def ask(self, question: str, *, source: str = "agent") -> uuid.UUID:
        question_id = uuid.uuid4()
        self._questions[question_id] = question
        self.asked.append((question, source))
        return question_id

    def answer(self, question_id: uuid.UUID) -> str | None:
        if question_id in self._answered:
            return self._answered[question_id]
        reply = self.answers(self._questions[question_id])
        if reply is not None:
            self._answered[question_id] = reply
        return reply

    def expire_question(self, question_id: uuid.UUID) -> None:
        self.expired_questions.append(question_id)

    def add_cost(self, usd: float) -> None:
        self.costs.append(usd)

    # convenience for assertions
    def statuses(self) -> list[str]:
        return [to for to, _ in self.transitions]

    def events_of(self, type: str) -> list[dict[str, Any]]:  # noqa: A002
        return [p for t, p in self.events if t == type]


# --- step builders (seed 01: paginate-off-by-one) -------------------------------------------

PLAN_ARGS: dict[str, Any] = {
    "summary": "Page 2 repeats the last item of page 1.",
    "root_cause": "start offset subtracts one for pages after the first",
    "steps": "remove the - 1 from the start offset",
    "files": ["pager.py"],
    "test_plan": "run the failing test, then the whole suite",
}
FIX_OLD = "start = (page - 1) * size - 1 if page > 1 else 0"
FIX_NEW = "start = (page - 1) * size"

Step = list[dict[str, Any]] | Exception


def run_tests_call(selector: str | None = FAILING_TEST, id: str = "tests1") -> dict[str, Any]:  # noqa: A002
    return (
        call("run_tests", id=id) if selector is None else call("run_tests", id, selector=selector)
    )


def investigate_steps() -> list[Step]:
    return [
        [call("read_file", "read1", path="pager.py")],
        [call("submit_plan", "plan1", **PLAN_ARGS)],
    ]


def edit_step() -> Step:
    return [call("edit_file", "edit1", path="pager.py", old=FIX_OLD, new=FIX_NEW)]


def fix_steps(selector: str | None = FAILING_TEST) -> list[Step]:
    return [edit_step(), [run_tests_call(selector)]]


def finish_steps() -> list[Step]:
    return [[call("complete_task", "done1", summary="Fixed the offset.")]]


def happy_steps() -> list[Step]:
    return [*investigate_steps(), *fix_steps(), *finish_steps()]


def observation(agent: WebAgent, tool_call_id: str) -> str:
    """Content of the tool message answering `tool_call_id`."""
    for message in agent.messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id:
            return str(message["content"])
    raise AssertionError(f"no observation for {tool_call_id}")


def run_agent(agent: WebAgent, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo": "octo/widgets",
        "failing_test": getattr(agent.config, "failing_test", None) or "",
        "agents_prose": "Run the tests with pytest.",
        "test_command": TEST_COMMAND,
    }
    kwargs.update(overrides)
    return agent.run("Page 2 of paginate repeats the last item of page 1.", **kwargs)


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_env(tmp_path: Path) -> FakeEnv:
    root = tmp_path / "repo"
    shutil.copytree(SEED_REPO, root)
    return FakeEnv(root)


@pytest.fixture
def fake_channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def make_agent(
    tmp_path: Path, fake_clock: FakeClock
) -> Callable[..., tuple[WebAgent, ScriptedModel, FakeEnv, FakeChannel]]:
    """Factory: `make_agent(steps, bash=…, channel=…, cost=…, **WebAgentConfig fields)`."""

    def make(
        steps: Sequence[Step],
        *,
        bash: BashScript | None = None,
        channel: FakeChannel | None = None,
        evidence: Any = AcceptAllEvidence(),  # noqa: B008 - explicit M4 opt-out (m5 §0.6)
        cost: float = 0.0,
        **cfg: Any,
    ) -> tuple[WebAgent, ScriptedModel, FakeEnv, FakeChannel]:
        root = tmp_path / "repo"
        if not root.exists():
            shutil.copytree(SEED_REPO, root)
        env = FakeEnv(root, bash)
        channel = channel if channel is not None else FakeChannel()
        model = ScriptedModel(steps, cost=cost)
        config: dict[str, Any] = {
            "test_command": TEST_COMMAND,
            "allow": ["python -m pytest*"],
            "failing_test": FAILING_TEST,
            "outputs_dir": tmp_path / "outputs",
        }
        config.update(cfg)
        extra: dict[str, Any] = {"evidence": evidence}
        agent = WebAgent(
            model,
            env,
            channel=channel,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            **extra,
            **config,
        )
        return agent, model, env, channel

    return make


# --- database helpers ----------------------------------------------------------------------


def make_db_user(db: Database) -> FakeUser:
    """A user (auth.users + profile) in `db`, usable with the `auth` fixture."""
    uid = uuid.uuid4()
    user = FakeUser(id=uid, github_id=uid.int % 2**40, login=f"user-{uid.hex[:8]}")
    meta = {"user_name": user.login, "provider_id": str(user.github_id)}
    with db.backend() as conn:
        conn.execute(
            "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
            [user.id, f"{user.login}@example.test", json.dumps(meta)],
        )
        store.ensure_profile(conn, user.id, user.login)
    return user


def queued_task(
    db: Database,
    *,
    user: FakeUser | None = None,
    status: str = "queued",
    memory_mb: int = 512,
    cpus: float = 0.5,
    failing_test: str | None = FAILING_TEST,
    description: str = "Page 2 of paginate repeats the last item of page 1.",
    image: str = "python:3.12-slim",
) -> store.Task:
    """Insert a user (unless given) and a task directly; returns the task row."""
    owner = user or make_db_user(db)
    preflight = PreflightResult(
        decision="admit",
        image=image,
        memory_mb=memory_mb,
        cpus=cpus,
        time_limit_minutes=60,
        queue_position=None,
        warnings=[],
    )
    with db.backend() as conn:
        return store.create_task(
            conn,
            owner.id,
            repo="octo/widgets",
            base_branch="main",
            description=description,
            failing_test=failing_test,
            day=datetime.now(UTC).date(),
            status=status,
            reservation_mb=memory_mb,
            reservation_cpu=cpus,
            preflight=preflight,
        )


def task_owner(db: Database, task_id: uuid.UUID) -> uuid.UUID:
    with db.backend() as conn:
        row = conn.execute("select user_id from public.tasks where id = %s", [task_id]).fetchone()
    assert row is not None
    return uuid.UUID(str(row["user_id"]))


def task_row(db: Database, task_id: uuid.UUID) -> dict[str, Any]:
    with db.backend() as conn:
        row = conn.execute("select * from public.tasks where id = %s", [task_id]).fetchone()
    assert row is not None
    return dict(row)


def status_events(db: Database, task_id: uuid.UUID) -> list[dict[str, Any]]:
    with db.backend() as conn:
        rows = conn.execute(
            "select payload from public.events where task_id = %s and type = 'status' order by seq",
            [task_id],
        ).fetchall()
    return [dict(r["payload"]) for r in rows]


def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 30.0, interval: float = 0.05, what: str = ""
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what or predicate}")
        time.sleep(interval)


class AutoDecider(threading.Thread):
    """Plays the task owner: approves pending approvals and answers open questions via `store`.

    `reject_kinds` are rejected instead; `answer` is the reply to every open question.
    """

    def __init__(
        self,
        db: Database,
        *,
        answer: str = "retry",
        reject_kinds: Sequence[str] = (),
        approve: bool = True,
    ) -> None:
        super().__init__(daemon=True)
        self.db = db
        self.answer = answer
        self.reject_kinds = set(reject_kinds)
        self.approve = approve
        self.stop_event = threading.Event()
        self.errors: list[BaseException] = []

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._once()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the test
                self.errors.append(exc)
            self.stop_event.wait(0.03)

    def _once(self) -> None:
        with self.db.backend() as conn:
            approvals = conn.execute(
                "select a.id, a.task_id, a.kind, t.user_id from public.approvals a"
                " join public.tasks t on t.id = a.task_id where a.status = 'pending'"
            ).fetchall()
            questions = conn.execute(
                "select q.id, q.task_id, t.user_id from public.questions q"
                " join public.tasks t on t.id = q.task_id"
                " where q.answer is null and q.expired_at is null"
            ).fetchall()
        for row in approvals if self.approve else []:
            decision = "reject" if row["kind"] in self.reject_kinds else "approve"
            with self.db.backend() as conn, contextlib.suppress(store.Conflict, store.NotFound):
                store.decide_approval(
                    conn,
                    row["user_id"],
                    row["task_id"],
                    row["id"],
                    decision=decision,  # type: ignore[arg-type]
                    comment="no thanks" if decision == "reject" else None,
                )
        for row in questions:
            with self.db.backend() as conn, contextlib.suppress(store.Conflict, store.NotFound):
                store.answer_question(
                    conn, row["user_id"], row["task_id"], row["id"], answer=self.answer
                )

    def __enter__(self) -> AutoDecider:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop_event.set()
        self.join(timeout=10)
