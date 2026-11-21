"""Fakes and builders for the M7 tests (m7-contract §7). Reuses `m4_fakes`.

`FakeJudge` stands in for the Portkey judge (no network). `load_corpus` reads the guard corpus and
removes the `@@@@` marker that keeps secrets non-contiguous in the file. `guarded_agent` builds a
`WebAgent` over the seed-01 workspace with a `Guardrails` object around a fake judge. Token
builders below assemble secret-shaped values from pieces so no test file holds a contiguous token.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from m4_fakes import (
    FAILING_TEST,
    SEED_REPO,
    TEST_COMMAND,
    BashScript,
    FakeChannel,
    FakeClock,
    FakeEnv,
    ScriptedModel,
    Step,
    auto_approve,
)

from swf.agent.web_agent import AcceptAllEvidence, WebAgent
from swf.guard import Guardrails, InputGuard, JudgeVerdict, Source

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests" / "corpus" / "guard_corpus.yaml"
MARKER = "@@@@"

# --- secret-shaped test values, assembled from pieces ---------------------------------------

GITHUB_TOKEN = "ghp" + "_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK_TOKEN = "xox" + "b-" + "123456789012-abcdefghijklmnop"
SK_KEY = "sk" + "-" + "proj-abcdefghijklmnopqrstuvwxyz0123456789"
GOOGLE_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
PRIVATE_KEY_HEADER = "-----BEGIN RSA PRIV" + "ATE KEY-----"
JWT = (
    "eyJ" + "hbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
PHONE = "+1 202 555 0143"
SSN = "123-45-6789"
CARD = "4111 1111 1111 1111"

# --- the fake judge ------------------------------------------------------------------------

BENIGN = JudgeVerdict(injection=False, toxicity=False, malicious=False)


def flagged(
    *, injection: bool = False, toxicity: bool = False, malicious: bool = False, reason: str = "x"
) -> JudgeVerdict:
    return JudgeVerdict(injection=injection, toxicity=toxicity, malicious=malicious, reason=reason)


class FakeJudge:
    """`GuardJudge`: the verdict of the first rule whose key is a substring of the text."""

    def __init__(
        self,
        rules: dict[str, JudgeVerdict] | None = None,
        *,
        raises: Exception | None = None,
        default: JudgeVerdict = BENIGN,
    ) -> None:
        self.rules = dict(rules or {})
        self.raises = raises
        self.default = default
        self.calls: list[tuple[str, str]] = []  # (text, source)

    def judge(self, text: str, *, source: Source) -> JudgeVerdict:
        self.calls.append((text, source))
        if self.raises is not None:
            raise self.raises
        for key, verdict in self.rules.items():
            if key in text:
                return verdict
        return self.default


# --- the corpus ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusItem:
    id: str
    category: str
    source: str
    expect: tuple[str, ...]
    text: str


def load_corpus(path: Path = CORPUS) -> list[CorpusItem]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        CorpusItem(
            id=str(raw["id"]),
            category=str(raw["category"]),
            source=str(raw["source"]),
            expect=tuple(raw["expect"]),
            text=str(raw["text"]).replace(MARKER, ""),
        )
        for raw in data["items"]
    ]


# --- a model that reports Portkey hook results (§13 A1) ------------------------------------

ABSENT: Any = object()  # "no `portkey_hooks` key in this message's extra"


class HookedModel(ScriptedModel):
    """`ScriptedModel` that sets `extra["portkey_hooks"]` per query (`ABSENT` skips a query)."""

    def __init__(self, steps: Sequence[Step], hooks: Sequence[Any]) -> None:
        super().__init__(steps)
        self.hooks = list(hooks)

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        message = super().query(messages, **kwargs)
        if self.hooks:
            value = self.hooks.pop(0)
            if value is not ABSENT:
                message["extra"]["portkey_hooks"] = value
        return message


# --- a guarded agent -----------------------------------------------------------------------

_DEFAULT: Any = object()


def _fresh_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="m7-repo-"))
    atexit.register(shutil.rmtree, root, True)
    repo = root / "repo"
    shutil.copytree(SEED_REPO, repo)
    return repo


def guarded_agent(
    steps: Sequence[Step],
    *,
    judge: Any = None,
    env: FakeEnv | None = None,
    approve: Callable[..., Any] = auto_approve,
    guard: Any = _DEFAULT,
    hooks: Sequence[Any] | None = None,
    bash: BashScript | None = None,
    **cfg: Any,
) -> tuple[WebAgent, FakeChannel, FakeEnv]:
    """A `WebAgent` with `Guardrails(InputGuard(judge))` (or `guard=` as given, `None` included).

    `hooks` (one entry per model query, `ABSENT` for none) makes the model report
    `extra["portkey_hooks"]`. `bash` scripts the default environment. `approve` is the channel's
    decision function; extra keyword arguments are `WebAgentConfig` fields. The agent's `clock`
    is a `FakeClock`, so approval timeouts do not sleep for real.
    """
    if env is None:
        env = FakeEnv(_fresh_workspace(), bash)
    channel = FakeChannel(decide=approve)
    clock = FakeClock()
    model = ScriptedModel(steps) if hooks is None else HookedModel(steps, hooks)
    if guard is _DEFAULT:
        guard = Guardrails(InputGuard(judge))
    config: dict[str, Any] = {
        "test_command": TEST_COMMAND,
        "allow": ["python -m pytest*"],
        "failing_test": FAILING_TEST,
        "outputs_dir": env.root.parent / "outputs",
    }
    config.update(cfg)
    agent = WebAgent(
        model,
        env,
        channel=channel,
        evidence=AcceptAllEvidence(),
        clock=clock,
        sleep=clock.sleep,
        guard=guard,
        **config,
    )
    return agent, channel, env


def guard_events(channel: FakeChannel, type: str) -> list[dict[str, Any]]:  # noqa: A002
    return channel.events_of(type)


def approval_kinds(channel: FakeChannel) -> list[str]:
    return [kind for kind, _ in channel.approvals.values()]


def guard_approvals(channel: FakeChannel) -> list[dict[str, Any]]:
    return [p for k, p in channel.approvals.values() if k == "guard_flag"]


__all__ = [
    "ABSENT",
    "AWS_KEY",
    "BENIGN",
    "CARD",
    "CORPUS",
    "GITHUB_TOKEN",
    "GOOGLE_KEY",
    "JWT",
    "MARKER",
    "PHONE",
    "PRIVATE_KEY_HEADER",
    "SK_KEY",
    "SLACK_TOKEN",
    "SSN",
    "CorpusItem",
    "FakeJudge",
    "HookedModel",
    "approval_kinds",
    "flagged",
    "guard_approvals",
    "guard_events",
    "guarded_agent",
    "load_corpus",
]
