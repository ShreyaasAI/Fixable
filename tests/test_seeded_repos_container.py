"""The 10 seeded repos inside the real sandbox (plan M2; replaces the host-run M0 check).

Per seed: copy `repo/` to a temp workspace, start a `SandboxEnv` on the image from AGENTS.md
behind the egress proxy, run the setup commands (exit 0), then
  - target test alone: exit 1 and "1 failed" (a missing pytest also exits 1: check the output),
  - everything but the target: exit 0,
  - after copying `reference/` over the workspace on the host: the full test command exits 0.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from swf.config import load_agents_file
from swf.egress import EgressConfig, EgressInfo, attach_task_network, detach_task_network
from swf.sandbox import SandboxEnv, SandboxSpec

pytestmark = [pytest.mark.docker, pytest.mark.network]

SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeded_repos"
SEEDS = sorted(p for p in SEEDS_DIR.iterdir() if p.is_dir()) if SEEDS_DIR.exists() else []


@pytest.fixture(params=SEEDS, ids=lambda p: p.name)
def seed(request: pytest.FixtureRequest) -> Path:
    path: Path = request.param
    return path


@pytest.fixture
def sandbox_for(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> Iterator[Callable[..., SandboxEnv]]:
    """Sandbox on its own task network behind the proxy; removed (then the network) afterwards."""
    made: list[SandboxEnv] = []
    task_ids: list[str] = []

    def make(seed: Path, image: str, workspace: Path, memory_mb: int, cpus: float) -> SandboxEnv:
        task_id = f"swf-test-{run_id}-{seed.name}"[:64]
        task_ids.append(task_id)
        net = attach_task_network(egress_config, task_id)
        env = SandboxEnv(
            SandboxSpec(
                task_id=task_id,
                image=image,
                workspace=workspace,
                proxy_url=net.proxy_url,
                network=net.name,
                memory_mb=memory_mb,
                cpus=cpus,
                command_timeout=240,
            )
        )
        made.append(env)
        return env

    yield make
    for env in made:
        env.cleanup()
    for task_id in task_ids:
        detach_task_network(egress_config, task_id)


def _run(env: SandboxEnv, command: str) -> dict[str, Any]:
    return env.execute({"command": command})


def _describe(result: dict[str, Any]) -> str:
    return f"rc={result['returncode']}\n{result['output']}"


def test_seed_lifecycle_in_container(
    seed: Path, tmp_path: Path, sandbox_for: Callable[..., SandboxEnv]
) -> None:
    meta = yaml.safe_load((seed / "seed.yaml").read_text(encoding="utf-8"))
    target = meta["target_test"]
    workspace = tmp_path / "repo"
    shutil.copytree(seed / "repo", workspace)
    cfg = load_agents_file(workspace).config

    env = sandbox_for(seed, cfg.image, workspace, cfg.resources.memory_mb, cfg.resources.cpus)

    for command in cfg.setup:
        setup = _run(env, command)
        assert setup["returncode"] == 0, f"setup {command!r}: {_describe(setup)}"

    # Exit code 1 = tests ran and failed; other codes (e.g. 4, test id not found) must not count.
    target_run = _run(env, f"{cfg.test} {target}")
    assert target_run["returncode"] == 1, _describe(target_run)
    assert "1 failed" in target_run["output"], _describe(target_run)

    rest = _run(env, f'{cfg.test} --deselect "{target}"')
    assert rest["returncode"] == 0, _describe(rest)
    assert "passed" in rest["output"], _describe(rest)

    # The reference fix is applied on the host; the bind mount makes it visible in the container.
    shutil.copytree(seed / "reference", workspace, dirs_exist_ok=True)
    fixed = _run(env, cfg.test)
    assert fixed["returncode"] == 0, _describe(fixed)
    assert "passed" in fixed["output"], _describe(fixed)
