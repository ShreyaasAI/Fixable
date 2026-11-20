"""Hermetic backend git: clone, branch, apply, commit, push (m6-contract §0.2, §0.3, §2).

Every git call gets an environment built from scratch (never inherited) and leading `-c`
options that switch off hooks, fsmonitor and credential helpers, so no system/global config,
template hook or repo-planted hook reaches a backend git process. The GitHub token reaches git
only through `SWF_GIT_TOKEN` in the subprocess env, read by a static `GIT_ASKPASS` script; it is
never in argv, a remote URL, `.git/config` or any file. Nothing goes through a shell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = [
    "AGENT_EMAIL",
    "AGENT_NAME",
    "GitOps",
    "GitOpsError",
    "InvalidRepoName",
    "askpass_script",
    "branch_name",
    "normalize_repo",
    "redact",
    "validate_repo_name",
]

AGENT_NAME = "SWF Agent"
AGENT_EMAIL = "swf-agent@users.noreply.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")
_BRANCH_RE = re.compile(r"^agent/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")
_AUTH_FAILED_RE = re.compile(
    r"authentication failed|could not read (?:username|password)|invalid username or password"
    r"|\b401\b",
    re.IGNORECASE,
)
_WINDOWS_ENV = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC")
_ASKPASS = (
    "#!/bin/sh\n"
    "case \"$1\" in Username*) printf '%s\\n' x-access-token ;; "
    "*) printf '%s\\n' \"$SWF_GIT_TOKEN\" ;; esac\n"
)

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class GitOpsError(Exception):
    """A git step failed; the message is already redacted (§0.13)."""

    def __init__(self, stage: str, message: str, *, auth_failed: bool = False) -> None:
        super().__init__(f"git {stage} failed: {message}")
        self.stage = stage
        self.auth_failed = auth_failed


class InvalidRepoName(ValueError):
    pass


def validate_repo_name(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise InvalidRepoName(f"invalid repository name {repo!r}")
    if repo.split("/", 1)[1] in (".", ".."):
        raise InvalidRepoName(f"invalid repository name {repo!r}")
    return repo


def normalize_repo(value: str) -> str:
    """`owner/name` or `https://github.com/owner/name[.git][/]` -> `owner/name` (validated)."""
    match = _GITHUB_URL_RE.fullmatch(value)
    return validate_repo_name(match.group(1) if match else value)


def branch_name(task_id: uuid.UUID) -> str:
    return f"agent/{uuid.UUID(str(task_id))}"


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


_askpass_lock = threading.Lock()
_askpass_path: Path | None = None


def askpass_script() -> Path:
    """The static askpass helper (no secret in it), written once per process."""
    global _askpass_path
    with _askpass_lock:
        if _askpass_path is None or not _askpass_path.exists():
            directory = Path(tempfile.mkdtemp(prefix="swf-askpass-"))
            os.chmod(directory, 0o700)
            path = directory / "askpass"
            path.write_text(_ASKPASS, encoding="ascii", newline="\n")
            os.chmod(path, 0o700)
            _askpass_path = path
        return _askpass_path


class GitOps:
    def __init__(
        self,
        *,
        clone_base: str = "https://github.com",
        allow_file: bool = False,
        runner: Runner = subprocess.run,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.clone_base = clone_base.rstrip("/")
        self.allow_file = allow_file
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.git = shutil.which("git") or "git"
        self._home = Path(tempfile.mkdtemp(prefix="swf-git-home-"))
        self._template = Path(tempfile.mkdtemp(prefix="swf-git-template-"))

    # --- environment and options -----------------------------------------------------------

    def env(self, token: str | None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self._home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        if sys.platform == "win32":
            for key in _WINDOWS_ENV:
                value = os.environ.get(key)
                if value:
                    env[key] = value
        if token is not None:
            env["GIT_ASKPASS"] = str(askpass_script())
            env["SWF_GIT_TOKEN"] = token
        return env

    def _common_opts(self) -> list[str]:
        opts = [
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
        ]  # fmt: skip
        if self.allow_file:
            opts += ["-c", "protocol.file.allow=always"]
        return opts

    def remote_url(self, repo: str) -> str:
        return f"{self.clone_base}/{validate_repo_name(repo)}.git"

    def _run(
        self,
        stage: str,
        args: Sequence[str],
        *,
        cwd: Path | None,
        token: str | None = None,
        extra_env: dict[str, str] | None = None,
        ok_codes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        env = self.env(token)
        if extra_env:
            env.update(extra_env)
        try:
            proc = self.runner(
                [self.git, *self._common_opts(), *args],
                cwd=None if cwd is None else str(cwd),
                env=env,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitOpsError(stage, "timed out") from exc
        except OSError as exc:
            raise GitOpsError(stage, redact(f"{type(exc).__name__}: {exc}", token)) from None
        if proc.returncode not in ok_codes:
            stderr = (proc.stderr or b"").decode("utf-8", "replace")
            tail = redact(stderr, token)[-500:]
            raise GitOpsError(
                stage,
                f"exit {proc.returncode}: {tail}",
                auth_failed=bool(_AUTH_FAILED_RE.search(stderr)),
            )
        return proc

    # --- operations ------------------------------------------------------------------------

    def clone(self, repo: str, branch: str, dest: Path, *, token: str) -> None:
        dest = Path(dest)
        if dest.exists():
            raise GitOpsError("clone", "destination already exists")
        url = self.remote_url(repo)
        try:
            self._run(
                "clone",
                [
                    "clone", "-q", f"--template={self._template}", "--no-tags",
                    "--single-branch", "--branch", branch, "--no-recurse-submodules",
                    "--", url, str(dest),
                ],
                cwd=None,
                token=token,
            )  # fmt: skip
        except GitOpsError:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    def head(self, path: Path) -> str:
        proc = self._run("rev-parse", ["rev-parse", "HEAD"], cwd=path)
        return proc.stdout.decode("ascii", "replace").strip()

    def prepare_branch(self, path: Path, *, branch: str, base_sha: str | None) -> None:
        if not _BRANCH_RE.fullmatch(branch):
            raise GitOpsError("checkout", "branch must be agent/<task-id>")
        if base_sha is not None and not _SHA_RE.fullmatch(base_sha):
            raise GitOpsError("checkout", "invalid base sha")
        self._run("checkout", ["checkout", "-q", "-b", branch, base_sha or "HEAD"], cwd=path)

    def apply(self, path: Path, patch_file: Path) -> None:
        patch = str(Path(patch_file).resolve())
        self._run("apply", ["apply", "--check", patch], cwd=path)
        self._run("apply", ["apply", patch], cwd=path)
        self._run("apply", ["add", "-A"], cwd=path)

    def has_staged_changes(self, path: Path) -> bool:
        proc = self._run("diff", ["diff", "--cached", "--quiet"], cwd=path, ok_codes=(0, 1))
        return proc.returncode == 1

    def commit(self, path: Path, message: str) -> str:
        identity = {
            "GIT_AUTHOR_NAME": AGENT_NAME,
            "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
            "GIT_COMMITTER_NAME": AGENT_NAME,
            "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
        }
        self._run(
            "commit",
            [
                "-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}",
                "-c", "commit.gpgsign=false",
                "commit", "-q", "--no-verify", "-m", message,
            ],
            cwd=path,
            extra_env=identity,
        )  # fmt: skip
        return self.head(path)

    def push(self, path: Path, branch: str, *, token: str) -> None:
        if not _BRANCH_RE.fullmatch(branch):
            raise GitOpsError("push", "branch must be agent/<task-id>")
        ref = f"refs/heads/{branch}"
        proc = self._run(
            "push",
            ["push", "--no-verify", "--porcelain", "origin", f"{ref}:{ref}"],
            cwd=path,
            token=token,
        )
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("!"):
                raise GitOpsError("push", redact(line, token)[-500:])
