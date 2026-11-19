"""Validate the 10 seeded bug repos (implementation plan M0; spec §10 acceptance set).

Layout per seed: seeded_repos/<name>/{repo/, reference/, seed.yaml}
- repo/       the buggy repository the agent will see (contains AGENTS.md)
- reference/  files that overwrite repo/ files to produce the known-good fix
- seed.yaml   harness-only metadata: target test id + task description
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from swf.config import load_agents_file

SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeded_repos"
SEEDS = sorted(p for p in SEEDS_DIR.iterdir() if p.is_dir()) if SEEDS_DIR.exists() else []


def _run(cmd: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # M0 runs on the host; substitute the current interpreter for a leading `python`.
    # Container-based runs replace this in M2.
    if cmd.startswith("python "):
        cmd = f'"{sys.executable}" ' + cmd[len("python ") :]
    # No bytecode cache: a same-size edit within the same second would otherwise reuse a
    # stale .pyc and produce false evidence (found in M0; must also apply to M5 re-runs).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=120, env=env
    )


def test_there_are_ten_seeds() -> None:
    assert len(SEEDS) == 10, [p.name for p in SEEDS]


@pytest.fixture(params=SEEDS, ids=lambda p: p.name)
def seed(request: pytest.FixtureRequest) -> Path:
    path: Path = request.param
    return path


def test_seed_metadata(seed: Path) -> None:
    meta = yaml.safe_load((seed / "seed.yaml").read_text(encoding="utf-8"))
    assert meta["target_test"].startswith("tests/")
    assert meta["description"].strip()
    assert (seed / "reference").is_dir()
    # Reference fixes must never be visible to the agent.
    assert not (seed / "repo" / "reference").exists()


def test_seed_lifecycle(seed: Path, tmp_path: Path) -> None:
    meta = yaml.safe_load((seed / "seed.yaml").read_text(encoding="utf-8"))
    target = meta["target_test"]
    work = tmp_path / "repo"
    shutil.copytree(seed / "repo", work)

    cfg = load_agents_file(work).config
    assert cfg.image  # multi-arch image declared

    baseline_target = _run(f"{cfg.test} {target}", work)
    # Exit code 1 = tests ran and failed. Other non-zero codes (e.g. 4 = test id not found)
    # must not count as a reproduced failure.
    assert baseline_target.returncode == 1, baseline_target.stdout + baseline_target.stderr

    baseline_rest = _run(f'{cfg.test} --deselect "{target}"', work)
    assert baseline_rest.returncode == 0, baseline_rest.stdout + baseline_rest.stderr

    shutil.copytree(seed / "reference", work, dirs_exist_ok=True)
    fixed = _run(cfg.test, work)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
