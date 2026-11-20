"""Fakes and builders for the M5 tests (m5-contract §7). Reuses `m4_fakes`.

`pager_bash` is a fake test runner over the seed-01 workspace: a run fails while `pager.py` still
contains the bug and passes once it does not, so baselines, re-baselines and re-runs behave
like the real thing without Docker. `scripted_bash` is the counter-driven alternative for tests
that need "baseline fails, post passes, re-run fails".
"""

from __future__ import annotations

import re
import shutil
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import FakeUser
from m4_fakes import (
    FAILING_TEST,
    FIX_NEW,
    FIX_OLD,
    PLAN_ARGS,
    SEED_REPO,
    TEST_COMMAND,
    BashScript,
    FakeChannel,
    FakeClock,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    task_owner,
)

from swf import store
from swf.agent.web_agent import WebAgent
from swf.db import Database
from swf.evidence import EvidenceRecord, TaskHarness

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "seeded_repos"
SUITE_COMMAND = TEST_COMMAND
OTHER_FAILURE = "tests/test_other.py::test_legacy"

# --- pytest output -------------------------------------------------------------------------


def fake_pytest_output(*failed: str) -> str:
    """Realistic `pytest -q` output; each id in `failed` gets a short-summary line."""
    if not failed:
        return ".." + " " * 60 + "[100%]\n2 passed in 0.02s\n"
    lines = ["F" + "." * (len(failed) % 3) + " " * 57 + "[100%]"]
    lines.append("=================================== FAILURES ===================================")
    for test in failed:
        name = test.rsplit("::", 1)[-1]
        lines += [f"{'_' * 20} {name} {'_' * 20}", "    assert 1 == 2", "E   AssertionError"]
    lines.append("=========================== short test summary info ============================")
    lines += [f"FAILED {test} - AssertionError" for test in failed]
    lines.append(f"{len(failed)} failed, 1 passed in 0.03s")
    return "\n".join(lines) + "\n"


# --- bash fakes ----------------------------------------------------------------------------


def is_harness(command: str) -> bool:
    return command.startswith("export PYTHONDONTWRITEBYTECODE=1;")


def is_target(command: str) -> bool:
    return FAILING_TEST in command


def is_suite(command: str) -> bool:
    return not is_target(command) and command.rstrip().endswith("pytest -q")


def is_harness_target(command: str) -> bool:
    return is_harness(command) and is_target(command)


def is_harness_suite(command: str) -> bool:
    return is_harness(command) and is_suite(command)


Matcher = str | Callable[[str], bool]
Rule = tuple[Matcher, Callable[[int], tuple[str, int]]]


def _result(output: str, returncode: int) -> dict[str, Any]:
    info = "infra failure: timed out" if returncode == -1 else ""
    return {"output": output, "returncode": returncode, "exception_info": info}


def scripted_bash(rules: Sequence[Rule]) -> Callable[[str], dict[str, Any]]:
    """`FakeEnv` bash callable. The first rule whose matcher (a substring, or a predicate on the
    command) matches answers with `fn(n)`, n = how many times that rule matched before (0-based);
    `fn` returns `(output, returncode)`; returncode -1 is an infra failure. No match: `("", 0)`."""
    counts = [0] * len(rules)

    def bash(command: str) -> dict[str, Any]:
        for i, (matcher, fn) in enumerate(rules):
            hit = matcher in command if isinstance(matcher, str) else matcher(command)
            if hit:
                n = counts[i]
                counts[i] += 1
                return _result(*fn(n))
        return _result("", 0)

    return bash


_WRITE_RE = re.compile(r"open\('([^']+)', ?'w'\)\.write\('([^']*)'\)")


def write_command(name: str, text: str = "x") -> str:
    """An approved-in-every-phase `python -c` that `pager_bash` executes as a host write."""
    return f"python -c \"open('{name}','w').write('{text}')\""


def pager_bash(root: Path) -> Callable[[str], dict[str, Any]]:
    """Fake runner over the seed-01 workspace at `root` (see the module docstring)."""

    def buggy() -> bool:
        return FIX_OLD in (root / "pager.py").read_text(encoding="utf-8")

    def bash(command: str) -> dict[str, Any]:
        written = _WRITE_RE.search(command)
        if written:
            (root / written.group(1)).write_text(written.group(2), encoding="utf-8", newline="\n")
            return _result("", 0)
        if "pytest" in command:
            if is_target(command):
                return _result(
                    *(
                        (fake_pytest_output(FAILING_TEST), 1)
                        if buggy()
                        else (fake_pytest_output(), 0)
                    )
                )
            if is_suite(command):
                return _result(
                    *(
                        (fake_pytest_output(FAILING_TEST), 1)
                        if buggy()
                        else (fake_pytest_output(), 0)
                    )
                )
            return _result(fake_pytest_output(), 0)  # any other selector
        return _result("", 0)

    return bash


# --- evidence sink and harness builders ----------------------------------------------------


class MemorySink:
    """`EvidenceSink` in memory; ids are 1, 2, …."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def add(self, record: EvidenceRecord) -> int:
        self.records.append(record)
        return len(self.records)

    def kinds(self) -> list[str]:
        return [r.kind for r in self.records]

    def of(self, kind: str) -> list[EvidenceRecord]:
        return [r for r in self.records if r.kind == kind]


def new_env(tmp_path: Path, bash: BashScript | None = None) -> FakeEnv:
    """A `FakeEnv` over a fresh copy of seed 01's repo at `tmp_path/repo`."""
    root = tmp_path / "repo"
    if not root.exists():
        shutil.copytree(SEED_REPO, root)
    return FakeEnv(root, bash if bash is not None else pager_bash(root))


def harness_for(
    env: FakeEnv,
    channel: FakeChannel,
    tmp_path: Path,
    *,
    failing_test: str | None = FAILING_TEST,
    test_command: str | None = TEST_COMMAND,
    **kwargs: Any,
) -> tuple[TaskHarness, MemorySink]:
    sink = MemorySink()
    harness = TaskHarness(
        env=env,
        channel=channel,
        sink=sink,
        repo=env.root,
        root=tmp_path / "ws",
        test_command=test_command,
        failing_test=failing_test,
        **kwargs,
    )
    return harness, sink


HarnessAgent = tuple[WebAgent, ScriptedModel, FakeEnv, FakeChannel, TaskHarness, MemorySink]


@pytest.fixture
def make_harness_agent(tmp_path: Path, fake_clock: FakeClock) -> Callable[..., HarnessAgent]:
    """Factory: `make_harness_agent(steps, bash=…, channel=…, failing_test=…, **cfg)`.

    Runs `run_baseline` + `take_start_snapshot` (so the agent starts at investigate with S0
    taken), then builds a real `WebAgent` with the harness as its evidence gate. `bash`
    defaults to `pager_bash` over the workspace.
    """

    def make(
        steps: Sequence[Step],
        *,
        bash: BashScript | None = None,
        channel: FakeChannel | None = None,
        failing_test: str | None = FAILING_TEST,
        cost: float = 0.0,
        **cfg: Any,
    ) -> HarnessAgent:
        env = new_env(tmp_path, bash)
        channel = channel if channel is not None else FakeChannel()
        harness, sink = harness_for(env, channel, tmp_path, failing_test=failing_test)
        harness.run_baseline()
        harness.take_start_snapshot()
        model = ScriptedModel(steps, cost=cost)
        config: dict[str, Any] = {
            "test_command": TEST_COMMAND,
            "allow": ["python -m pytest*"],
            "failing_test": failing_test,
            "outputs_dir": tmp_path / "outputs",
        }
        config.update(cfg)
        agent = WebAgent(
            model,
            env,
            channel=channel,
            evidence=harness,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            **config,
        )
        return agent, model, env, channel, harness, sink

    return make


# --- seeded-repo steps ---------------------------------------------------------------------


def seed_target(seed: str) -> str:
    data = yaml.safe_load((SEEDS / seed / "seed.yaml").read_text(encoding="utf-8"))
    return str(data["target_test"])


def seed_description(seed: str) -> str:
    data = yaml.safe_load((SEEDS / seed / "seed.yaml").read_text(encoding="utf-8"))
    return str(data["description"])


def seed_reference_files(seed: str) -> list[str]:
    ref = SEEDS / seed / "reference"
    return sorted(p.relative_to(ref).as_posix() for p in ref.rglob("*") if p.is_file())


def reference_steps(seed: str) -> list[Step]:
    """read_file, submit_plan, one write_file per reference file, the target test (A1 switch),
    the whole suite, complete_task. Harness-only: reads `seeded_repos/<seed>/reference/`."""
    files = seed_reference_files(seed)
    target = seed_target(seed)
    plan = {**PLAN_ARGS, "files": files}
    steps: list[Step] = [
        [call("read_file", path=files[0])],
        [call("submit_plan", **plan)],
    ]
    for rel in files:
        content = (SEEDS / seed / "reference" / rel).read_bytes().decode("utf-8")
        steps.append([call("write_file", path=rel, content=content)])
    steps += [
        [call("run_tests", selector=target)],
        [call("run_tests")],
        [call("complete_task", summary="Fixed the bug.")],
    ]
    return steps


def fix_and_finish_steps() -> list[Step]:
    """Execute + verify + finish for seed 01 (the happy tail after an approved plan)."""
    return [
        [call("edit_file", "edit1", path="pager.py", old=FIX_OLD, new=FIX_NEW)],
        [call("run_tests", "t_target", selector=FAILING_TEST)],
        [call("run_tests", "t_suite")],
        [call("complete_task", "done1", summary="Fixed the offset.")],
    ]


# --- database helpers ----------------------------------------------------------------------


def owner_user(db: Database, task_id: uuid.UUID) -> FakeUser:
    return FakeUser(id=task_owner(db, task_id), github_id=1, login="owner")


def evidence_rows(db: Database, task_id: uuid.UUID) -> list[store.EvidenceRow]:
    user = owner_user(db, task_id)
    with db.as_user(user.claims()) as conn:
        return store.list_evidence(conn, user.id, task_id)


def events_of_type(db: Database, task_id: uuid.UUID, type: str) -> list[dict[str, Any]]:  # noqa: A002
    with db.backend() as conn:
        rows = conn.execute(
            "select payload from public.events where task_id = %s and type = %s order by seq",
            [task_id, type],
        ).fetchall()
    return [dict(r["payload"]) for r in rows]


def approvals_of(db: Database, task_id: uuid.UUID) -> list[tuple[str, str]]:
    """(kind, status) of the task's approvals, oldest first."""
    with db.backend() as conn:
        rows = conn.execute(
            "select kind, status from public.approvals where task_id = %s order by created_at, id",
            [task_id],
        ).fetchall()
    return [(r["kind"], r["status"]) for r in rows]


class ApproveOnly(threading.Thread):
    """Plays the owner for the listed approval kinds only; every other approval stays pending."""

    def __init__(self, db: Database, kinds: Sequence[str]) -> None:
        super().__init__(daemon=True)
        self.db = db
        self.kinds = set(kinds)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            with self.db.backend() as conn:
                rows = conn.execute(
                    "select a.id, a.task_id, a.kind, t.user_id from public.approvals a"
                    " join public.tasks t on t.id = a.task_id where a.status = 'pending'"
                ).fetchall()
            for row in rows:
                if row["kind"] in self.kinds:
                    try:
                        with self.db.backend() as conn:
                            store.decide_approval(
                                conn,
                                row["user_id"],
                                row["task_id"],
                                row["id"],
                                decision="approve",
                                comment=None,
                            )
                    except (store.Conflict, store.NotFound):
                        pass
            self.stop_event.wait(0.03)

    def __enter__(self) -> ApproveOnly:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop_event.set()
        self.join(timeout=10)


def write_source(tmp_path: Path, *, agents_md: bool = True, name: str = "source") -> Path:
    """A copy of seed 01 for `LocalCopyWorkspace`: AGENTS.md without setup commands (or none)."""
    source = tmp_path / name
    shutil.copytree(SEED_REPO, source)
    if not agents_md:
        (source / "AGENTS.md").unlink()
        return source
    config = {
        "image": "python:3.12-slim",
        "setup": [],
        "test": TEST_COMMAND,
        "allow": ["python -m pytest*"],
        "resources": {"memory_mb": 512, "cpus": 0.5},
    }
    text = "# Pager\n\nRun the tests with pytest.\n\n```yaml agent-config\n"
    text += yaml.safe_dump(config) + "```\n"
    (source / "AGENTS.md").write_text(text, encoding="utf-8", newline="\n")
    return source
