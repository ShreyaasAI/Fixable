"""M6 live proof (m6-contract §8): one real PR on a throwaway GitHub repo.

Opt-in: needs `SWF_TEST_REPO` (`owner/name` or a github.com URL; the throwaway repo, for example
`ShreyaasAI/swf-pr-sandbox`) and an authenticated `gh` (its token is read at test time, held in a
local variable only, and never printed or stored). The PR and branch are left open as evidence.

    PYTHONDONTWRITEBYTECODE=1 uv run pytest -m live tests/test_m6_live_pr.py -rs
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from m4_fakes import queued_task
from m5_fakes import events_of_type
from m6_fakes import scan_for
from swf.gitops import GitOps, branch_name, normalize_repo
from swf.pr import GitHubPublisher, GitHubWorkspace, PrRequest

from swf import store
from swf.db import Database
from swf.evidence import copy_tree, pr_patch
from swf.github import GitHubClient

pytest_plugins = ["m4_fakes", "m5_fakes"]
pytestmark = [pytest.mark.live, pytest.mark.network]

FORBIDDEN_TARGET = "shreyaasai/fixable"  # the project's own remote: never a push target
GH_FALLBACK = "C:/Program Files/GitHub CLI/gh.exe"


def gh_token() -> str:
    gh = shutil.which("gh") or (GH_FALLBACK if Path(GH_FALLBACK).is_file() else None)
    if gh is None:
        pytest.skip("gh is not installed")
    try:
        run = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("gh auth token could not run")
    token = run.stdout.strip()
    if run.returncode != 0 or not token:
        pytest.skip("gh is not authenticated (gh auth token failed)")
    return token


def test_a_real_pr_is_opened_on_the_throwaway_repo(fresh_db: Database, tmp_path: Path) -> None:
    raw = os.environ.get("SWF_TEST_REPO")
    if not raw:
        pytest.skip("SWF_TEST_REPO is not set")
    repo = normalize_repo(raw)
    if repo.lower() == FORBIDDEN_TARGET:
        pytest.fail("SWF_TEST_REPO must not be the project's own remote")
    token = gh_token()
    api = GitHubClient()
    meta = api.get_repo(token, repo)
    if meta.size_kb == 0:
        pytest.skip(f"{repo} is empty; add a README first")
    if not meta.can_push:
        pytest.skip(f"the gh token cannot push to {repo}")

    task = queued_task(fresh_db)
    with fresh_db.backend() as conn:
        conn.execute(
            "update public.tasks set repo = %s, base_branch = %s where id = %s",
            [repo, meta.default_branch, task.id],
        )
    task = task.model_copy(update={"repo": repo, "base_branch": meta.default_branch})

    tokens = lambda _task: token  # noqa: E731
    git = GitOps()
    workspaces = tmp_path / "workspaces"
    ws = GitHubWorkspace(git, tokens, workspaces).prepare(task)
    assert ws.base_sha
    copy_tree(ws.repo, ws.root / "baseline")
    marker = ws.repo / "swf-live" / f"{task.id}.txt"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(f"SWF M6 live test for task {task.id}\n".encode())
    patch = pr_patch(ws.root / "baseline", ws.repo)
    assert f"swf-live/{task.id}.txt" in patch

    request = PrRequest(
        task_id=task.id,
        repo=repo,
        base_branch=task.base_branch,
        base_sha=ws.base_sha,
        root=ws.root,
        patch=patch,
        title=f"SWF live test {task.id}",
        body="Opened by the SWF M6 live test. Safe to close and delete the branch.",
    )
    result = GitHubPublisher(git, api, tokens).publish(request, task)
    with fresh_db.backend() as conn:
        store.record_pr(
            conn,
            task.id,
            url=result.url,
            number=result.number,
            branch=result.branch,
            commit_sha=result.commit_sha,
        )

    assert re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/\d+", result.url)
    assert result.branch == branch_name(task.id)
    with fresh_db.backend() as conn:
        stored = conn.execute("select pr_url from public.tasks where id = %s", [task.id]).fetchone()
    assert stored is not None and stored["pr_url"] == result.url
    assert len(events_of_type(fresh_db, task.id, "pr_opened")) == 1

    # Independent check, made from the test and not from src.
    got = httpx.get(
        f"https://api.github.com/repos/{repo}/pulls/{result.number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "swf-m6-live-test",
        },
        timeout=30,
    )
    assert got.status_code == 200
    pull = got.json()
    assert pull["head"]["ref"] == branch_name(task.id)
    assert pull["state"] == "open"
    assert pull["merged"] is False
    assert scan_for(tmp_path, token.encode()) == []
