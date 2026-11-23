"""M9 T10 live check (m9-contract §0.24): the 10 `seed/<name>` branches exist on the eval repo.

Read-only: `gh api` GETs only. Opt-in: needs `SWF_TEST_REPO` (the eval repo, for example
`ShreyaasAI/swf-pr-sandbox`) and an authenticated `gh`.

    SWF_TEST_REPO=ShreyaasAI/swf-pr-sandbox PYTHONDONTWRITEBYTECODE=1 \
        uv run pytest -o addopts="" -q -p no:cacheprovider -m live tests/test_m9_eval_live.py -rs
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from swf.gitops import normalize_repo
from swf_eval.seeds import FORBIDDEN_REPO, load_seeds, seed_branch

pytestmark = [pytest.mark.live, pytest.mark.network]

GH_FALLBACK = "C:/Program Files/GitHub CLI/gh.exe"
SEEDS = load_seeds()


def _gh() -> str:
    gh = shutil.which("gh") or (GH_FALLBACK if Path(GH_FALLBACK).is_file() else None)
    if gh is None:
        pytest.skip("gh is not installed")
    return gh


def _repo() -> str:
    raw = os.environ.get("SWF_TEST_REPO")
    if not raw:
        pytest.skip("SWF_TEST_REPO is not set")
    repo = normalize_repo(raw)
    if repo.lower() == FORBIDDEN_REPO:
        pytest.fail("SWF_TEST_REPO must not be the project's own remote")
    return repo


def _get(gh: str, path: str, *, raw: bool = False) -> subprocess.CompletedProcess[bytes]:
    cmd = [gh, "api", "--method", "GET"]
    if raw:
        cmd += ["-H", "Accept: application/vnd.github.raw"]
    return subprocess.run([*cmd, path], capture_output=True, timeout=60)


def test_there_are_ten_local_seeds() -> None:
    assert len(SEEDS) == 10


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: s.name)
def test_seed_branch_exists_with_the_local_agents_md(seed) -> None:  # type: ignore[no-untyped-def]
    gh, repo = _gh(), _repo()
    branch = seed_branch(seed)
    found = _get(gh, f"repos/{repo}/branches/{branch}")
    assert found.returncode == 0, f"{branch} is missing on {repo}: {found.stderr[-300:]!r}"
    remote = _get(gh, f"repos/{repo}/contents/AGENTS.md?ref={branch}", raw=True)
    assert remote.returncode == 0, f"AGENTS.md missing at {branch}: {remote.stderr[-300:]!r}"
    assert remote.stdout == (seed.repo_dir / "AGENTS.md").read_bytes()
