"""Fakes and builders for the M6 tests (m6-contract §8). Reuses `m4_fakes` and `m5_fakes`.

Everything here talks to git with plain subprocess calls and a scrubbed environment, never through
`swf.gitops`, so the tests can check `GitOps` against an independent oracle. Imports of `swf.pr`
and `swf.gitops` are local to the functions that need them.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from swf import store

SENTINEL_TOKEN = "ghs_SWFSENTINEL" + "0123456789abcdef0123456789abcd"
REPO = "octo/widgets"
OWNER, NAME = REPO.split("/")
BASE_BRANCH = "main"
HOOK_NAMES = (
    "post-checkout",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-push",
    "reference-transaction",
    "post-index-change",
    "post-merge",
)
DEFAULT_FILES: dict[str, str] = {
    "README.md": "# widgets\n",
    "pager.py": "def paginate(items, page, size):\n    return items[:size]\n",
    "src/pkg/mod.py": "VALUE = 1\n",
    "notes.txt": "no newline at end",
    "empty.txt": "",
}
AUTHOR = ("SWF Agent", "swf-agent@users.noreply.github.com")


# --- plain git (independent oracle) ---------------------------------------------------------


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Seed",
        GIT_AUTHOR_EMAIL="seed@example.test",
        GIT_COMMITTER_NAME="Seed",
        GIT_COMMITTER_EMAIL="seed@example.test",
        LC_ALL="C",
    )
    return env


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run plain git hermetically; returns stdout (text)."""
    run = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        check=False,
    )
    if check and run.returncode != 0:
        raise AssertionError(f"git {args} failed: {run.stderr.decode(errors='replace')}")
    return run.stdout.decode("utf-8", errors="replace").strip()


def write_files(root: Path, files: Mapping[str, str | bytes | None]) -> None:
    """Write (or, for None, delete) files under `root`; LF endings, no translation."""
    for rel, content in files.items():
        path = root / rel
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under `root` (posix relpath -> bytes), `.git` excluded."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or not path.is_file():
            continue
        out[rel.as_posix()] = path.read_bytes()
    return out


def bare_remote(
    tmp_path: Path,
    files: Mapping[str, str] | None = None,
    *,
    seed: str | None = None,
    branch: str = BASE_BRANCH,
    repo: str = REPO,
) -> tuple[str, Path]:
    """A bare remote laid out as `<base>/<owner>/<name>.git`. Returns `(file:// base, bare path)`.
    The content is `files`, or `seeded_repos/<seed>/repo/`, or `DEFAULT_FILES`."""
    remote_root = tmp_path / "remote"
    work = tmp_path / f"remote-work-{uuid.uuid4().hex[:6]}"
    work.mkdir(parents=True)
    git("init", "-q", "-b", branch, cwd=work)
    if seed is not None:
        from m5_fakes import SEEDS

        for src in (SEEDS / seed / "repo").rglob("*"):
            if src.is_file() and "__pycache__" not in src.parts:
                rel = src.relative_to(SEEDS / seed / "repo")
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, work / rel)
    else:
        write_files(work, files if files is not None else DEFAULT_FILES)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    bare = remote_root / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--bare", str(work), str(bare))
    rmtree_force(work)
    return remote_root.as_uri(), bare


def _make_writable_and_retry(func: Any, path: str, exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree_force(path: Path) -> None:
    """rmtree that copes with read-only git pack files on Windows."""
    shutil.rmtree(path, onexc=_make_writable_and_retry)


def clone_plain(base: str, repo: str, dest: Path, branch: str = BASE_BRANCH) -> None:
    git("clone", "-q", "--branch", branch, f"{base}/{repo}.git", str(dest))


def make_patch(base: str, tmp_path: Path, edits: Mapping[str, str | bytes | None]) -> str:
    """A git-made patch (`a/` `b/` headers) for `edits` against the remote's base branch."""
    work = tmp_path / f"patch-work-{uuid.uuid4().hex[:6]}"
    clone_plain(base, REPO, work)
    write_files(work, edits)
    git("add", "-A", cwd=work)
    return git("diff", "--cached", "--no-color", cwd=work) + "\n"


def ref_sha(bare: Path, ref: str) -> str | None:
    out = git("--git-dir", str(bare), "rev-parse", "--verify", "-q", ref, check=False)
    return out or None


def refs(bare: Path) -> set[str]:
    out = git("--git-dir", str(bare), "for-each-ref", "--format=%(refname)")
    return set(out.split())


def commit_info(bare: Path, ref: str) -> dict[str, str]:
    fmt = "%an|%ae|%cn|%ce|%P|%s"
    an, ae, cn, ce, parents, subject = git(
        "--git-dir", str(bare), "log", "-1", f"--format={fmt}", ref
    ).split("|", 5)
    return {
        "author": f"{an} <{ae}>",
        "committer": f"{cn} <{ce}>",
        "parents": parents,
        "subject": subject,
    }


def bare_tree(bare: Path, ref: str) -> dict[str, bytes]:
    """File contents at `ref` in the bare repo."""
    names = git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", ref).splitlines()
    out: dict[str, bytes] = {}
    for name in names:
        run = subprocess.run(
            ["git", "--git-dir", str(bare), "show", f"{ref}:{name}"],
            env=_clean_env(),
            capture_output=True,
            check=True,
        )
        out[name] = run.stdout
    return out


def scan_for(root: Path, needle: bytes) -> list[Path]:
    """Every file under `root` (symlinks not followed) whose bytes contain `needle`."""
    hits: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                if needle in path.read_bytes():
                    hits.append(path)
            except OSError:
                continue
    return hits


# --- hook traps -----------------------------------------------------------------------------


def _trap_script(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f'#!/bin/sh\necho ran > "{marker.as_posix()}"\nexit 0\n'.encode())
    path.chmod(0o755)


def _trap_hooks(directory: Path, markers: Path) -> None:
    for name in HOOK_NAMES:
        _trap_script(directory / name, markers / name)


def trap_pushurl(markers: Path) -> Path:
    return markers.parent / "trap-pushurl.git"


def plant_hook_traps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Global/system/template config that would run hooks, fsmonitor, a credential helper or a
    filter. Returns the markers dir: every trap writes a file there when it runs."""
    markers = tmp_path / "markers"
    markers.mkdir()
    trap = tmp_path / "trap"
    hooks = trap / "hooks"
    template = trap / "template"
    _trap_hooks(hooks, markers)
    _trap_hooks(template / "hooks", markers)
    for name in ("fsmonitor", "credential", "filter"):
        _trap_script(trap / f"{name}.sh", markers / name)
    config = (
        f"[core]\n\thooksPath = {hooks.as_posix()}\n"
        f"\tfsmonitor = {(trap / 'fsmonitor.sh').as_posix()}\n"
        f"[init]\n\ttemplateDir = {template.as_posix()}\n"
        f"[credential]\n\thelper = !sh {(trap / 'credential.sh').as_posix()}\n"
        f'[filter "evil"]\n\tclean = sh {(trap / "filter.sh").as_posix()}\n'
        f"\tsmudge = sh {(trap / 'filter.sh').as_posix()}\n"
    )
    global_cfg = trap / "global.gitconfig"
    global_cfg.write_bytes(config.encode())
    home = trap / "home"
    home.mkdir()
    (home / ".gitconfig").write_bytes(config.encode())
    xdg = trap / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_bytes(config.encode())
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(global_cfg))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ambient")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ambient@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Ambient")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ambient@example.test")
    return markers


def plant_workspace_traps(repo: Path, markers: Path) -> None:
    """Traps inside `repo/.git`: hooks, and a config pointing hooksPath, fsmonitor, the credential
    helper and the push URL at traps (`trap_pushurl(markers)` must never come to exist)."""
    trap = markers.parent / "ws-trap"
    _trap_hooks(repo / ".git" / "hooks", markers)
    _trap_hooks(trap / "hooks", markers)
    for name in ("fsmonitor", "credential"):
        _trap_script(trap / f"{name}.sh", markers / name)
    with (repo / ".git" / "config").open("ab") as fh:
        fh.write(
            (
                f"[core]\n\thooksPath = {(trap / 'hooks').as_posix()}\n"
                f"\tfsmonitor = {(trap / 'fsmonitor.sh').as_posix()}\n"
                f"[credential]\n\thelper = !sh {(trap / 'credential.sh').as_posix()}\n"
                f'[remote "origin"]\n\tpushurl = {trap_pushurl(markers).as_uri()}\n'
            ).encode()
        )


def marker_names(markers: Path) -> list[str]:
    return sorted(p.name for p in markers.iterdir())


# --- fake GitHub REST -----------------------------------------------------------------------


@dataclass
class FakeGitHub:
    """`httpx.MockTransport` for the REST API: records `(method, path, json)` and answers from
    `script` (one `(status, body)` per call, then the default 201 for `POST /repos/<r>/pulls`)."""

    script: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        self.tokens.append(request.headers.get("authorization", ""))
        if self.script:
            status, payload = self.script.pop(0)
            return httpx.Response(status, json=payload)
        repo = request.url.path.removeprefix("/repos/").removesuffix("/pulls")
        return httpx.Response(
            201, json={"number": 1, "html_url": f"https://github.com/{repo}/pull/1"}
        )

    def client(self) -> Any:
        from swf.github import GitHubClient

        return GitHubClient(transport=httpx.MockTransport(self.handler))

    @property
    def pulls(self) -> list[dict[str, Any]]:
        return [c[2] for c in self.calls if c[0] == "POST" and c[1].endswith("/pulls")]


# --- tasks, requests, publishers ------------------------------------------------------------


def make_task(**overrides: Any) -> store.Task:
    """A `store.Task` that never touched a database."""
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "repo": REPO,
        "base_branch": BASE_BRANCH,
        "description": "Page 2 of paginate repeats the last item.",
        "failing_test": "tests/test_pager.py::test_second_page",
        "status": "opening_pr",
        "phase": "pr",
        "reservation_mb": 512,
        "reservation_cpu": 0.5,
        "cost_usd": 0.0,
        "pr_url": None,
        "preflight": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return store.Task(**fields)


def base_sha_of(bare: Path, branch: str = BASE_BRANCH) -> str:
    sha = ref_sha(bare, f"refs/heads/{branch}")
    assert sha
    return sha


def make_request(
    task: store.Task, root: Path, patch: str, base_sha: str | None, **overrides: Any
) -> Any:
    from swf.pr import PrRequest

    fields: dict[str, Any] = {
        "task_id": task.id,
        "repo": task.repo,
        "base_branch": task.base_branch,
        "base_sha": base_sha,
        "root": root,
        "patch": patch,
        "title": "SWF: fix the pager",
        "body": "body",
    }
    fields.update(overrides)
    return PrRequest(**fields)


def new_publisher(
    base: str,
    gh: FakeGitHub,
    *,
    retries: list[tuple[str, int, str]] | None = None,
    token: str = SENTINEL_TOKEN,
    **kwargs: Any,
) -> Any:
    from swf.gitops import GitOps
    from swf.pr import GitHubPublisher

    return GitHubPublisher(
        GitOps(clone_base=base, allow_file=True),
        gh.client(),
        lambda _task: token,
        sleep=lambda _s: None,
        on_retry=(lambda step, n, err: retries.append((step, n, err)))
        if retries is not None
        else None,
        **kwargs,
    )


def new_git_ops(base: str) -> Any:
    from swf.gitops import GitOps

    return GitOps(clone_base=base, allow_file=True)


def argv_runner(
    calls: list[tuple[list[str], dict[str, str]]], stdout: bytes = b"abc123\n", rc: int = 0
) -> Any:
    """A `GitOps(runner=…)` that records `(argv, env)` and answers `rc` / `stdout`."""

    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return subprocess.CompletedProcess(list(argv), rc, stdout, b"")

    return run
