"""Seed repos and their `seed/<name>` branches (m9-contract §0.8, §1.5)."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from swf.gitops import GitOps, GitOpsError, normalize_repo, redact

REPO_ROOT = Path(__file__).resolve().parents[2]
SEEDED_REPOS = REPO_ROOT / "seeded_repos"
FORBIDDEN_REPO = "shreyaasai/fixable"
ALLOWED_EVAL_REPOS = frozenset({"ShreyaasAI/swf-pr-sandbox"})
SEED_BRANCH_RE = re.compile(r"^seed/[0-9]{2}-[a-z0-9-]{1,60}$", re.ASCII)
SEED_DATE = "2026-11-22T00:00:00Z"
SEED_NAME = "SWF Eval"
SEED_EMAIL = "swf-eval@users.noreply.github.com"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git")


@dataclass(frozen=True)
class Seed:
    name: str
    target_test: str
    description: str
    repo_dir: Path  # seeded_repos/<name>/repo   (pristine; what the agent sees)
    reference_dir: Path  # seeded_repos/<name>/reference (harness-only)


class SeedBranchConflict(Exception):
    """The remote `seed/<name>` branch exists at another commit; it is never forced."""


def load_seeds(root: Path = SEEDED_REPOS) -> list[Seed]:
    seeds = []
    for meta in sorted(root.glob("*/seed.yaml")):
        data = yaml.safe_load(meta.read_text(encoding="utf-8"))
        seeds.append(
            Seed(
                name=meta.parent.name,
                target_test=str(data["target_test"]),
                description=str(data["description"]),
                repo_dir=meta.parent / "repo",
                reference_dir=meta.parent / "reference",
            )
        )
    return seeds


def check_eval_repo(repo: str) -> str:
    """The canonical allowed name, or ValueError (Fixable and every other repo)."""
    name = normalize_repo(repo)
    for allowed in ALLOWED_EVAL_REPOS:
        if name.lower() == allowed.lower() and name.lower() != FORBIDDEN_REPO:
            return allowed
    raise ValueError(f"repository {name} is not an allowed eval repository")


def seed_branch(seed: Seed) -> str:
    return f"seed/{seed.name}"


def _check_branch(branch: str) -> str:
    if not isinstance(branch, str) or not SEED_BRANCH_RE.fullmatch(branch):
        raise ValueError(f"not a seed branch: {branch!r}")
    return branch


def push_seed_branch(git: GitOps, path: Path, branch: str, *, token: str) -> None:
    """Push exactly refs/heads/<b>:refs/heads/<b>; never forced. `GitOps.push` is agent-only."""
    ref = f"refs/heads/{_check_branch(branch)}"
    proc = git._run(
        "push",
        ["push", "--no-verify", "--porcelain", "origin", f"{ref}:{ref}"],
        cwd=path,
        token=token,
        ok_codes=(0, 1),
    )
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("!"):
            raise SeedBranchConflict(redact(line, token)[-500:])
    if proc.returncode != 0:
        tail = redact((proc.stderr or b"").decode("utf-8", "replace"), token)[-500:]
        raise GitOpsError("push", f"exit {proc.returncode}: {tail}")


def _commit_seed(git: GitOps, seed: Seed, path: Path, branch: str) -> str:
    shutil.copytree(seed.repo_dir, path, ignore=IGNORE)
    template = tempfile.mkdtemp(prefix="swf-eval-template-")
    opts = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
    git._run("init", [*opts, "init", "-q", f"--template={template}", "-b", branch], cwd=path)
    git._run("add", [*opts, "add", "-A"], cwd=path)
    identity = {
        "GIT_AUTHOR_NAME": SEED_NAME,
        "GIT_AUTHOR_EMAIL": SEED_EMAIL,
        "GIT_AUTHOR_DATE": SEED_DATE,
        "GIT_COMMITTER_NAME": SEED_NAME,
        "GIT_COMMITTER_EMAIL": SEED_EMAIL,
        "GIT_COMMITTER_DATE": SEED_DATE,
    }
    git._run(
        "commit",
        [
            *opts, "-c", f"user.name={SEED_NAME}", "-c", f"user.email={SEED_EMAIL}",
            "-c", "commit.gpgsign=false",
            "commit", "-q", "--no-verify", "-m", f"seed: {seed.name}",
        ],
        cwd=path,
        extra_env=identity,
    )  # fmt: skip
    return git.head(path)


def publish_seed_branches(
    git: GitOps, repo: str, token: str, seeds: Sequence[Seed], work: Path
) -> dict[str, str]:
    """One deterministic root commit per seed's `repo/`, pushed as `seed/<name>`."""
    repo = check_eval_repo(repo)
    shas: dict[str, str] = {}
    for seed in seeds:
        branch = _check_branch(seed_branch(seed))
        path = Path(work) / seed.name
        sha = _commit_seed(git, seed, path, branch)
        git._run("remote", ["remote", "add", "origin", git.remote_url(repo)], cwd=path)
        proc = git._run(
            "ls-remote", ["ls-remote", "origin", f"refs/heads/{branch}"], cwd=path, token=token
        )
        remote = proc.stdout.decode("ascii", "replace").split()
        if not remote:
            push_seed_branch(git, path, branch, token=token)
        elif remote[0] != sha:
            raise SeedBranchConflict(f"{branch} exists on the remote at another commit")
        shas[seed.name] = sha
    return shas
