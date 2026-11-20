"""DoD 4: there is no merge call (m6-contract §0.7, §3, §8). A static scan of the source, plus the
exact public surface of `GitHubClient`."""

from __future__ import annotations

import re
from pathlib import Path

from swf.github import GitHubClient

SRC = Path(__file__).resolve().parent.parent / "src" / "swf"
# policy.py legitimately lists `git merge` among the commands the agent may not run.
EXEMPT = {"policy.py"}
FORBIDDEN = {
    "merge endpoint": re.compile(r"/merge\b"),
    "merge_method": re.compile(r"merge_method"),
    "auto merge": re.compile(r"auto_?merge", re.IGNORECASE),
    "graphql auto merge": re.compile(r"enablePullRequestAutoMerge"),
    "merge literal": re.compile(r"""["']merge["']"""),
}
KNOWN_METHODS = {"get_user", "list_repos", "get_repo", "branch_exists", "get_file", "create_pull"}


def test_no_source_file_can_merge_a_pull_request() -> None:
    files = [p for p in SRC.rglob("*.py") if p.name not in EXEMPT]
    assert files, "source tree not found"
    hits = [
        f"{p.relative_to(SRC)}: {label}"
        for p in files
        for label, pattern in FORBIDDEN.items()
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_the_github_client_surface_is_exactly_the_known_set() -> None:
    public = {
        name
        for name in dir(GitHubClient)
        if not name.startswith("_") and callable(getattr(GitHubClient, name))
    }
    assert public == KNOWN_METHODS
