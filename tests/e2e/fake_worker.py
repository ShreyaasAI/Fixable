"""E2E worker launcher (m10a §4.2): the real worker and a real Docker sandbox, with a scripted
model, a local-copy workspace of one seed and a fake pull-request publisher.

The script is chosen per task, lazily on the first model query, by a tag in the task description
(`[e2e:fix]`, `[e2e:ask]`, `[e2e:slow]`): the description reaches the model in the first user
message. Run by `LocalStack(worker_argv=[sys.executable, <this file>])`.
"""

from __future__ import annotations

import logging
import os
import sys
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from swf import _mswea  # noqa: E402

_mswea.isolate()  # FIRST: before anything imports minisweagent (m4-contract §1)

import fakes  # noqa: E402
from m4_fakes import ScriptedModel, Step, call  # noqa: E402
from m5_fakes import reference_steps  # noqa: E402

from swf.db import Database  # noqa: E402
from swf.egress import EgressConfig, ensure_egress  # noqa: E402
from swf.guard import Guardrails  # noqa: E402
from swf.pr import PrRequest, PrResult  # noqa: E402
from swf.sandbox import cleanup_orphans, sandbox_user  # noqa: E402
from swf.settings import AbuseLimits, Settings  # noqa: E402
from swf.store import Task  # noqa: E402
from swf.worker import (  # noqa: E402
    LocalCopyWorkspace,
    Worker,
    WorkerConfig,
    build_runner_deps,
    serve_until_signalled,
)

logger = logging.getLogger("e2e.worker")


def script_for(text: str) -> list[Step]:
    """The scripted steps for the tag found in `text` (the messages of the first query)."""
    fix = reference_steps(fakes.SEED)
    if fakes.TAG_SLOW in text:
        return [[call("bash", "slow1", command=fakes.SLOW_COMMAND)]]
    if fakes.TAG_ASK in text:
        return [[call("ask_user", "ask1", question=fakes.ASK_QUESTION)], *fix]
    # `[e2e:fix]` (and the default): long output first (test 10), then the reference fix
    return [[call("bash", "long1", command=fakes.LONG_OUTPUT_COMMAND)], *fix]


def _text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        parts.append(content if isinstance(content, str) else repr(content))
    return "\n".join(parts)


class TaggedModel(ScriptedModel):
    """A `ScriptedModel` whose steps are picked on the first query from the task description."""

    def __init__(self) -> None:
        super().__init__([])
        self._chosen = False

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        if not self._chosen:
            self.steps = script_for(_text(messages))
            self._chosen = True
        return super().query(messages, **kwargs)


class FakePublisher:
    """`PullRequestPublisher` that opens nothing and reports a fixed URL."""

    def publish(self, req: PrRequest, task: Task) -> PrResult:
        return PrResult(url=fakes.PR_URL, number=1, branch=f"agent/{task.id}", commit_sha="0" * 40)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sandbox_user()  # fail fast on a root host, before any Docker work
    settings = Settings.from_env()
    workspaces_root = Path(os.environ["SWF_WORKSPACES_ROOT"]).resolve()
    egress = EgressConfig()
    ensure_egress(egress)
    config = WorkerConfig(
        worker_id=f"e2e-{os.getpid()}",
        workspaces_root=workspaces_root,
        max_concurrent=3,
        poll_seconds=1.0,
        abuse=AbuseLimits.from_env(),
        agent={"poll_seconds": 0.2, "step_limit": 60},
        sweep_seconds=2.0,
    )
    db = Database(settings.database_url, max_size=config.max_concurrent * 2 + 2)
    db.open()
    try:
        deps = build_runner_deps(
            settings,
            db,
            config,
            egress,
            model_factory=TaggedModel,
            workspaces=LocalCopyWorkspace(fakes.SEED_REPO_DIR, workspaces_root),
            publisher=FakePublisher(),
            guard_factory=Guardrails.off,
        )
        logger.info("e2e worker ready")
        serve_until_signalled(Worker(deps, orphan_cleaner=partial(cleanup_orphans, egress=egress)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
