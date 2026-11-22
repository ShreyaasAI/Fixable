"""Close the eval's PRs and delete their `agent/<task-id>` branches (m9-contract §0.3, §1.5)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from swf.github import GitHubClient, GitHubError, GitHubNotFound
from swf_eval.metrics import TaskRecord
from swf_eval.seeds import check_eval_repo

_AGENT_BRANCH_RE = re.compile(r"^agent/[0-9a-f-]{36}$", re.ASCII)


def _pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def cleanup_plan(records: Sequence[TaskRecord], repo: str) -> list[tuple[int, str]]:
    """(PR number, branch) of each record whose pr_url is exactly this repo's /pull/<n>."""
    repo = check_eval_repo(repo)
    url_re = re.compile(rf"^https://github\.com/{re.escape(repo)}/pull/([1-9][0-9]*)$", re.ASCII)
    plan: list[tuple[int, str]] = []
    for r in records:
        match = url_re.fullmatch(r.pr_url or "")
        branch = f"agent/{r.task_id}"
        if match and _AGENT_BRANCH_RE.fullmatch(branch):
            plan.append((int(match.group(1)), branch))
    return plan


def apply_cleanup(
    api: GitHubClient, token: str, repo: str, plan: Sequence[tuple[int, str]]
) -> list[str]:
    """Close + delete only what a fresh GET proves is the harness's own PR; one log line each."""
    repo = check_eval_repo(repo)
    log: list[str] = []
    for number, branch in plan:
        try:
            pull = api._get_pull(token, repo, number)
        except GitHubNotFound:
            log.append(f"skip PR {number}: not found")
            continue
        except GitHubError as exc:
            log.append(f"skip PR {number}: {exc}")
            continue
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        base_repo = str((base.get("repo") or {}).get("full_name") or "")
        if pull.get("html_url") != _pr_url(repo, number):
            log.append(f"skip PR {number}: html_url differs from the recorded URL")
        elif head_repo.lower() != repo.lower() or base_repo.lower() != repo.lower():
            log.append(f"skip PR {number}: head or base repo is not {repo}")
        elif head.get("ref") != branch:
            log.append(f"skip PR {number}: head branch is not {branch}")
        else:
            try:
                api.close_pull(token, repo, number)
                log.append(f"closed PR {number}")
                api.delete_branch(token, repo, branch)
                log.append(f"deleted branch {branch}")
            except GitHubError as exc:
                log.append(f"PR {number}: {exc}")
    return [ln.replace(token, "***") if token else ln for ln in log]
