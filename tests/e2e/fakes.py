"""Shared fake data for the e2e launchers and tests (m10a §4.2). Imported by the launcher
scripts (`fake_api.py`, `fake_worker.py`), which run as separate processes, and by the tests."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:  # `helpers`, `m4_fakes`, ... live one level up
    sys.path.insert(0, str(TESTS_DIR))

from helpers import FakeGitHub, FakeRegistry, index_manifest, repo_json  # noqa: E402

GITHUB_ID = 424242
GITHUB_LOGIN = "e2e-dev"
PROVIDER_TOKEN = "e2e-provider-token"  # what the dev sign-in form posts as the GitHub token
REPO = "e2e/repo"
READONLY_REPO = "e2e/readonly"
MISSING_BRANCH = "no-such-branch"
SEED = "01-paginate-off-by-one"
SEED_REPO_DIR = ROOT / "seeded_repos" / SEED / "repo"  # the agent sees only `repo/`
PR_URL = "https://github.com/e2e/repo/pull/1"

TAG_FIX = "[e2e:fix]"
TAG_ASK = "[e2e:ask]"
TAG_SLOW = "[e2e:slow]"
LONG_OUTPUT_COMMAND = "ls -R /usr/lib | head -n 100"  # allowed by the policy; > 40 lines
SLOW_COMMAND = "tail -f /dev/null"  # allowed by the policy; runs until the task is cancelled
ASK_QUESTION = "Should the fix keep the page size unchanged?"


def make_github() -> FakeGitHub:
    """The dev user (matching the LocalStack JWTs) with one pushable and one read-only repo."""
    gh = FakeGitHub(
        users={PROVIDER_TOKEN: (GITHUB_ID, GITHUB_LOGIN)},
        repos={
            PROVIDER_TOKEN: [
                repo_json(REPO, push=True),
                repo_json(READONLY_REPO, push=False),
            ]
        },
        page_size=30,
    )
    gh.repo_host.readonly_repos.add(READONLY_REPO)
    gh.repo_host.missing_branches.add(MISSING_BRANCH)
    # AGENTS.md: the default one (python:3.12-slim, 512 MB, 0.5 cpu)
    return gh


def make_registry() -> FakeRegistry:
    """python:3.12-slim as a multi-arch image (linux/amd64 and linux/arm64)."""
    reg = FakeRegistry()
    reg.manifests[("registry-1.docker.io", "library/python", "3.12-slim")] = index_manifest(
        "linux/amd64", "linux/arm64/v8"
    )
    return reg
