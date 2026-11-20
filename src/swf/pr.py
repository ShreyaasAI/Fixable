"""GitHub workspace, PR publisher and PR body (m6-contract §4).

The workspace clones the task's repo before the container exists. The publisher applies the
evidence patch to a *fresh* clone (never the agent-writable `repo/`), commits on the
server-computed branch `agent/<task-id>`, pushes only that ref (never forced) and opens a PR.
There is no code path that merges: a human merges in GitHub.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

import httpx

from swf import store
from swf.db import Database
from swf.evidence import HARNESS_PREAMBLE, EvidenceRecord
from swf.github import GitHubAuthError, GitHubClient, GitHubError, GitHubUnprocessable
from swf.gitops import GitOps, GitOpsError, branch_name
from swf.tokens import TokenCipher, TokenDecryptError

if TYPE_CHECKING:
    from swf.worker import TaskWorkspace

__all__ = [
    "GitHubPublisher",
    "GitHubWorkspace",
    "PrRequest",
    "PrResult",
    "PrSummary",
    "PublishError",
    "PullRequestPublisher",
    "TokenSource",
    "TokenUnavailable",
    "db_token_source",
    "pr_title",
    "render_pr_body",
]

BODY_LIMIT = 60000
TITLE_LIMIT = 72
_DESCRIPTION_LIMIT = 2000
_FILES_LIMIT = 100
_FAILURES_LIMIT = 20
_CELL_LIMIT = 500


class TokenUnavailable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


TokenSource = Callable[[store.Task], str]


def db_token_source(db: Database, cipher: TokenCipher) -> TokenSource:
    def source(task: store.Task) -> str:
        with db.backend() as conn:
            blob = store.get_task_github_token(conn, task.id)
        if blob is None:
            raise TokenUnavailable("github_token_missing")
        try:
            return cipher.decrypt(blob)
        except TokenDecryptError:
            raise TokenUnavailable("github_token_invalid") from None

    return source


# --- workspace -----------------------------------------------------------------------------


class GitHubWorkspace:
    """`worker.WorkspaceProvider` that clones the task's repo into `<root>/<id>/repo`."""

    def __init__(self, git: GitOps, tokens: TokenSource, workspaces_root: Path) -> None:
        self.git = git
        self.tokens = tokens
        self.workspaces_root = Path(workspaces_root)

    def prepare(self, task: store.Task) -> TaskWorkspace:
        from swf.worker import TaskWorkspace

        root = (self.workspaces_root / str(task.id)).resolve()
        repo = root / "repo"
        if repo.exists():
            shutil.rmtree(repo)
        root.mkdir(parents=True, exist_ok=True)
        token = self.tokens(task)
        try:
            self.git.clone(task.repo, task.base_branch, repo, token=token)
        except GitOpsError as exc:
            if exc.auth_failed:
                raise GitHubAuthError("GitHub rejected the token") from None
            raise
        return TaskWorkspace(root=root, repo=repo, base_sha=self.git.head(repo))


# --- publisher -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PrRequest:
    task_id: uuid.UUID
    repo: str
    base_branch: str
    base_sha: str | None
    root: Path
    patch: str
    title: str
    body: str


@dataclass(frozen=True)
class PrResult:
    url: str
    number: int
    branch: str
    commit_sha: str


class PublishError(Exception):
    def __init__(self, stage: str, message: str = "") -> None:
        super().__init__(f"{stage}: {message}" if message else stage)
        self.stage = stage


class PullRequestPublisher(Protocol):
    def publish(self, req: PrRequest, task: store.Task) -> PrResult: ...


_T = TypeVar("_T")


class GitHubPublisher:
    def __init__(
        self,
        git: GitOps,
        api: GitHubClient,
        tokens: TokenSource,
        *,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        backoff: Sequence[float] = (1, 4, 10),
        on_retry: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self.git = git
        self.api = api
        self.tokens = tokens
        self.max_retries = max(1, max_retries)
        self.sleep = sleep
        self.backoff = tuple(backoff)
        self.on_retry = on_retry

    def _retry(self, step: str, fn: Callable[[], _T], retry_on: tuple[type[Exception], ...]) -> _T:
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except GitOpsError as exc:
                if exc.auth_failed:
                    raise GitHubAuthError("GitHub rejected the token") from None
                error = str(exc)
            except (GitHubAuthError, GitHubUnprocessable):
                raise
            except retry_on as exc:
                error = f"{type(exc).__name__}: {exc}"
            if self.on_retry is not None:
                self.on_retry(step, attempt, error)
            if attempt < self.max_retries and self.backoff:
                self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        raise PublishError(step, "retries exhausted")

    def publish(self, req: PrRequest, task: store.Task) -> PrResult:
        token = self.tokens(task)
        if not req.patch.strip():
            raise PublishError("empty_diff")
        branch = branch_name(req.task_id)
        root = Path(req.root)
        pr = root / "pr"

        def clone() -> None:
            if pr.exists():
                shutil.rmtree(pr)
            self.git.clone(req.repo, req.base_branch, pr, token=token)

        self._retry("clone", clone, ())
        patch_file = root / "pr.patch"
        try:
            self.git.prepare_branch(pr, branch=branch, base_sha=req.base_sha)
            patch_file.write_text(
                req.patch, encoding="utf-8", errors="surrogateescape", newline="\n"
            )
            self.git.apply(pr, patch_file)
            staged = self.git.has_staged_changes(pr)
        except GitOpsError as exc:
            raise PublishError("apply", str(exc)) from None
        if not staged:
            raise PublishError("empty_diff")
        try:
            commit_sha = self.git.commit(pr, f"{req.title}\n\nSWF task {req.task_id}")
        except GitOpsError as exc:
            raise PublishError("apply", str(exc)) from None
        self._retry("push", lambda: self.git.push(pr, branch, token=token), ())

        def create() -> PrResult:
            pull = self.api.create_pull(
                token, req.repo, head=branch, base=req.base_branch, title=req.title, body=req.body
            )
            return PrResult(
                url=pull.html_url, number=pull.number, branch=branch, commit_sha=commit_sha
            )

        try:
            return self._retry("create_pr", create, (GitHubError, httpx.HTTPError))
        except GitHubUnprocessable as exc:
            raise PublishError("create_pr", str(exc)) from None


# --- title and body ------------------------------------------------------------------------


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ellipsise(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pr_title(task: store.Task) -> str:
    if task.failing_test:
        title = f"SWF: fix {_one_line(task.failing_test)}"
    else:
        lines = task.description.strip().splitlines()
        title = f"SWF: {_one_line(lines[0]) if lines else 'agent change'}"
    return _ellipsise(title, TITLE_LIMIT)


@dataclass(frozen=True)
class PrSummary:
    task_id: uuid.UUID
    description: str
    failing_test: str | None
    test_command: str | None
    baseline: EvidenceRecord | None
    baseline_suite: EvidenceRecord | None
    post: EvidenceRecord | None
    suite: EvidenceRecord | None
    rerun: EvidenceRecord | None
    files: list[str] = field(default_factory=list)
    base_sha: str | None = None


def _code(text: str, limit: int = _CELL_LIMIT) -> str:
    """Inline code for a table cell: one line, no backticks or pipes breaking out."""
    clean = _ellipsise(_one_line(text).replace("`", "'"), limit).replace("|", "\\|")
    return f"`{clean}`" if clean else ""


def _row(step: str, record: EvidenceRecord | None) -> str:
    if record is None:
        return f"| {step} | (not run) | | |"
    command = record.command or ""
    if command.startswith(HARNESS_PREAMBLE):
        command = command[len(HARNESS_PREAMBLE) :]
    failures = sorted(record.failures)
    shown = ", ".join(_code(f, 200) for f in failures[:_FAILURES_LIMIT])
    if len(failures) > _FAILURES_LIMIT:
        shown += f" … and {len(failures) - _FAILURES_LIMIT} more"
    exit_code = "" if record.exit_code is None else str(record.exit_code)
    return f"| {step} | {_code(command)} | {exit_code} | {shown} |"


def render_pr_body(summary: PrSummary) -> str:
    """Evidence summary only: commands, exit codes, failure ids, files; never output (§0.8)."""
    description = _ellipsise(summary.description.strip(), _DESCRIPTION_LIMIT)
    quoted = "\n".join(f"> {line}" for line in description.splitlines()) or "> (none)"
    lines = ["## What", "", quoted, ""]
    if summary.failing_test:
        lines.append(f"Target test: {_code(summary.failing_test)}")
    if summary.test_command:
        lines.append(f"Test command: {_code(summary.test_command)}")
    lines += [
        "",
        "## Evidence",
        "",
        "| Step | Command | Exit code | Failures |",
        "|---|---|---|---|",
        _row("Baseline (target)", summary.baseline),
        _row("Baseline (suite)", summary.baseline_suite),
        _row("Post-fix (target)", summary.post),
        _row("Post-fix (suite)", summary.suite),
        _row("Harness re-run", summary.rerun),
        "",
        "## Files changed",
        "",
    ]
    lines += [f"- {_code(path, 300)}" for path in summary.files[:_FILES_LIMIT]]
    if len(summary.files) > _FILES_LIMIT:
        lines.append(f"- … and {len(summary.files) - _FILES_LIMIT} more")
    base = summary.base_sha[:12] if summary.base_sha else "unknown"
    lines += [
        "",
        "---",
        f"Opened by SWF for task {summary.task_id} on base {base}. "
        "The agent never merges; review and merge in GitHub.",
    ]
    return "\n".join(lines)[: BODY_LIMIT - 1] + "\n"
