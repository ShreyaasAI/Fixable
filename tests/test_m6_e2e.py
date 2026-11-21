"""M6 end to end with a real sandbox (m6-contract §8): a seed cloned from a local bare remote, the
reference fix scripted as the model, a PR branch pushed back, and no token anywhere the agent can
look. Needs Docker and network (pip install through the egress proxy). Docker objects are named
`swf-test-<run>-…`."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import AutoDecider, ScriptedModel, call, queued_task, task_row
from m5_fakes import (
    reference_steps,
    seed_description,
    seed_reference_files,
    seed_target,
)
from m6_fakes import (
    SENTINEL_TOKEN,
    FakeGitHub,
    bare_remote,
    base_sha_of,
    git,
    refs,
    scan_for,
)

from swf.db import Database
from swf.egress import EgressConfig, EgressInfo
from swf.gitops import GitOps, branch_name
from swf.pr import GitHubPublisher, GitHubWorkspace
from swf.sandbox import open_task_sandbox
from swf.worker import EnvRequest, RunnerDeps, Worker, WorkerConfig

pytest_plugins = ["m4_fakes", "m5_fakes"]
pytestmark = [pytest.mark.docker, pytest.mark.network]

SEED = "01-paginate-off-by-one"
# The grep pattern and the echoed words are split so the command text itself never matches.
LOOK_AROUND = (
    "env; cat /repo/.git/config; "
    "grep -rl 'ghs_''SWFSENTINEL' /repo && echo LEAK_\"\"FOUND || echo NO_\"\"LEAK"
)


class RecordingModel(ScriptedModel):
    """Keeps the message list of the last query, so the test can read every observation."""

    def __init__(self, steps: Any) -> None:
        super().__init__(steps)
        self.messages: list[dict[str, Any]] = []

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.messages = messages
        return super().query(messages, **kwargs)


def leftovers(run_id: str) -> list[str]:
    found: list[str] = []
    for args in (["ps", "-a"], ["network", "ls"]):
        out = subprocess.run(
            [
                "docker",
                *args,
                "--filter",
                "label=swf.managed=1",
                "--format",
                '{{.Label "swf.task"}}',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        found += [
            line.strip()
            for line in out.stdout.splitlines()
            if line.strip().startswith(f"swf-test-{run_id}-")
        ]
    return found


def test_a_seed_is_cloned_fixed_and_pushed_with_no_token_in_the_workspace(
    fresh_db: Database,
    egress: EgressInfo,
    egress_config: EgressConfig,
    run_id: str,
    tmp_path: Path,
) -> None:
    base, bare = bare_remote(tmp_path, seed=SEED)
    base_sha = base_sha_of(bare)
    workspaces = tmp_path / "workspaces"
    gh = FakeGitHub()
    git_ops = GitOps(clone_base=base, allow_file=True)
    tokens = lambda _task: SENTINEL_TOKEN  # noqa: E731
    model_holder: list[RecordingModel] = []

    steps = reference_steps(SEED)
    steps = [steps[0], [call("bash", "look1", command=LOOK_AROUND)], *steps[1:]]

    def env_factory(request: EnvRequest) -> Any:
        named = dataclasses.replace(
            request, task_id=f"swf-test-{run_id}-{request.task_id[:12]}", worker_id=f"w-{run_id}"
        )
        return open_task_sandbox(egress_config, named)

    def model_factory() -> RecordingModel:
        model = RecordingModel(steps)
        model_holder.append(model)
        return model

    deps = RunnerDeps(
        db=fresh_db,
        workspaces=GitHubWorkspace(git_ops, tokens, workspaces),
        env_factory=env_factory,
        model_factory=model_factory,
        config=WorkerConfig(
            worker_id=f"w-{run_id}",
            workspaces_root=workspaces,
            agent={"poll_seconds": 0.05, "step_limit": 60},
        ),
        publisher=GitHubPublisher(git_ops, gh.client(), tokens, sleep=lambda _s: None),
    )
    task = queued_task(
        fresh_db,
        failing_test=seed_target(SEED),
        description=seed_description(SEED),
        memory_mb=512,
        cpus=0.5,
    )
    worker = Worker(deps, orphan_cleaner=lambda *args, **kwargs: [])
    with AutoDecider(fresh_db):
        assert worker.run_once() == task.id

    row = task_row(fresh_db, task.id)
    assert row["status"] == "completed", row["error"]
    # what the agent saw
    (model,) = model_holder
    seen = json.dumps(model.messages)
    assert "NO_LEAK" in seen
    assert "LEAK_FOUND" not in seen
    assert SENTINEL_TOKEN not in seen
    assert "SWF_GIT_TOKEN" not in seen
    # what was pushed
    branch = f"refs/heads/{branch_name(task.id)}"
    assert refs(bare) == {"refs/heads/main", branch}
    changed = set(git("--git-dir", str(bare), "diff", "--name-only", base_sha, branch).split())
    assert changed == set(seed_reference_files(SEED))
    # what was recorded
    assert row["pr_url"] == "https://github.com/octo/widgets/pull/1"
    with fresh_db.backend() as conn:
        events = conn.execute(
            "select 1 from public.events where task_id = %s and type = 'pr_opened'", [task.id]
        ).fetchall()
    assert len(events) == 1
    # what is on disk
    assert scan_for(workspaces, SENTINEL_TOKEN.encode()) == []
    assert leftovers(run_id) == []
