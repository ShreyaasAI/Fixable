"""Seed branches (m9-contract §0.8, §1.5): `publish_seed_branches` and `push_seed_branch` against a
local bare repo (no network), and the unchanged backend guard `GitOps.push` (agent/<uuid> only).

The oracle is plain git (`m6_fakes`), never `swf.gitops`. The remote's reflog is switched on, so a
push that moves a ref leaves a line and one that does not leaves nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from m6_fakes import (
    SENTINEL_TOKEN,
    argv_runner,
    bare_remote,
    bare_tree,
    git,
    marker_names,
    new_git_ops,
    plant_hook_traps,
    ref_sha,
    refs,
)
from m9_eval_fakes import ALLOWED_REPO, tree_files
from swf_eval.seeds import (
    Seed,
    SeedBranchConflict,
    load_seeds,
    publish_seed_branches,
    push_seed_branch,
    seed_branch,
)

from swf.gitops import GitOps, GitOpsError

TASK_BRANCH = f"agent/{uuid.UUID(int=1)}"


@pytest.fixture(scope="module")
def two_seeds() -> list[Seed]:
    return [s for s in load_seeds() if s.name.startswith(("01-", "07-"))]


@pytest.fixture
def remote(tmp_path: Path) -> tuple[str, Path]:
    """`(file:// base, bare path)` of an `ShreyaasAI/swf-pr-sandbox` remote with a `main` branch."""
    base, bare = bare_remote(tmp_path, repo=ALLOWED_REPO)
    git("--git-dir", str(bare), "config", "core.logAllRefUpdates", "true")
    return base, bare


def work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def reflog_lines(bare: Path, branch: str) -> int:
    path = bare / "logs" / "refs" / "heads" / branch
    return len(path.read_text("utf-8").splitlines()) if path.exists() else 0


def ref(branch: str) -> str:
    return f"refs/heads/{branch}"


# --- publish_seed_branches -----------------------------------------------------------------


def test_branch_name_is_seed_slash_name(two_seeds: list[Seed]) -> None:
    assert [seed_branch(s) for s in two_seeds] == [f"seed/{s.name}" for s in two_seeds]


def test_each_seed_becomes_a_branch_holding_exactly_its_repo_files(
    tmp_path: Path, remote: tuple[str, Path], two_seeds: list[Seed]
) -> None:
    base, bare = remote
    shas = publish_seed_branches(
        new_git_ops(base), ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, work_dir(tmp_path)
    )
    assert set(shas) == {s.name for s in two_seeds}
    for seed in two_seeds:
        branch = ref(seed_branch(seed))
        assert ref_sha(bare, branch) == shas[seed.name]
        tree = bare_tree(bare, branch)
        assert tree == tree_files(seed.repo_dir)  # same names, same bytes (LF untouched)
        assert "seed.yaml" not in tree
        assert not any(name.startswith("reference") for name in tree)
        assert not any(name.endswith(".pyc") or "__pycache__" in name for name in tree)
    assert ref_sha(bare, "refs/heads/main") is not None  # main untouched
    assert refs(bare) == {"refs/heads/main", *(ref(seed_branch(s)) for s in two_seeds)}


def test_the_seed_commit_is_a_single_root_commit_with_the_fixed_dates(
    tmp_path: Path, remote: tuple[str, Path], two_seeds: list[Seed]
) -> None:
    base, bare = remote
    publish_seed_branches(
        new_git_ops(base), ALLOWED_REPO, SENTINEL_TOKEN, two_seeds[:1], work_dir(tmp_path)
    )
    branch = ref(seed_branch(two_seeds[0]))
    assert git("--git-dir", str(bare), "rev-list", "--count", branch) == "1"
    assert git("--git-dir", str(bare), "log", "-1", "--format=%P", branch) == ""
    fixed = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp())
    assert (
        git("--git-dir", str(bare), "log", "-1", "--format=%at|%ct", branch) == f"{fixed}|{fixed}"
    )


def test_the_commit_is_deterministic_across_remotes_and_work_dirs(
    tmp_path: Path, remote: tuple[str, Path], two_seeds: list[Seed]
) -> None:
    base, _bare = remote
    first = publish_seed_branches(
        new_git_ops(base), ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, work_dir(tmp_path)
    )
    other = tmp_path / "other"
    other.mkdir()
    base2, _bare2 = bare_remote(other, repo=ALLOWED_REPO)
    work2 = other / "elsewhere"
    work2.mkdir()
    second = publish_seed_branches(
        new_git_ops(base2), ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, work2
    )
    assert first == second


def test_republishing_returns_the_same_shas_and_moves_nothing(
    tmp_path: Path, remote: tuple[str, Path], two_seeds: list[Seed]
) -> None:
    base, bare = remote
    ops = new_git_ops(base)
    first = publish_seed_branches(ops, ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, work_dir(tmp_path))
    logged = {s.name: reflog_lines(bare, seed_branch(s)) for s in two_seeds}
    assert all(n == 1 for n in logged.values())  # control: the first publish did write the refs
    again = tmp_path / "again"
    again.mkdir()
    second = publish_seed_branches(ops, ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, again)
    assert second == first
    assert {s.name: reflog_lines(bare, seed_branch(s)) for s in two_seeds} == logged


def test_a_branch_pointing_at_another_commit_is_a_conflict_and_the_remote_is_unchanged(
    tmp_path: Path, remote: tuple[str, Path], two_seeds: list[Seed]
) -> None:
    base, bare = remote
    seed = two_seeds[0]
    branch = ref(seed_branch(seed))
    other_commit = ref_sha(bare, "refs/heads/main")
    assert other_commit
    git("--git-dir", str(bare), "update-ref", branch, other_commit)
    before_refs = refs(bare)
    with pytest.raises(SeedBranchConflict) as err:
        publish_seed_branches(
            new_git_ops(base), ALLOWED_REPO, SENTINEL_TOKEN, [seed], work_dir(tmp_path)
        )
    assert SENTINEL_TOKEN not in str(err.value)
    assert ref_sha(bare, branch) == other_commit  # never forced
    assert refs(bare) == before_refs


@pytest.mark.parametrize(
    "repo",
    [
        "shreyaasai/FIXABLE",
        "ShreyaasAI/Fixable",
        "https://github.com/ShreyaasAI/Fixable.git",
        "ShreyaasAI/swf-ci",
        "other/swf-pr-sandbox",
    ],
)
def test_a_disallowed_repo_starts_no_git_process(
    tmp_path: Path, two_seeds: list[Seed], repo: str
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    ops = GitOps(clone_base=tmp_path.as_uri(), allow_file=True, runner=argv_runner(calls))
    with pytest.raises(ValueError):
        publish_seed_branches(ops, repo, SENTINEL_TOKEN, two_seeds, work_dir(tmp_path))
    assert calls == []
    assert not any(work_dir(tmp_path).iterdir())


def test_template_global_and_system_hooks_never_run_when_publishing(
    tmp_path: Path,
    remote: tuple[str, Path],
    two_seeds: list[Seed],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, bare = remote
    markers = plant_hook_traps(tmp_path, monkeypatch)
    shas = publish_seed_branches(
        new_git_ops(base), ALLOWED_REPO, SENTINEL_TOKEN, two_seeds, work_dir(tmp_path)
    )
    assert ref_sha(bare, ref(seed_branch(two_seeds[0]))) == shas[two_seeds[0].name]
    assert marker_names(markers) == []


# --- push_seed_branch ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        TASK_BRANCH,
        "agent/x",
        "seed/../x",
        "+seed/01-a",
        "main",
        "refs/heads/seed/01-a",
        "seed/1-a",
        "seed/01-A",
        "seed/01-a/b",
        "seed/01-a\n",
        "",
    ],
)
def test_push_seed_branch_refuses_any_branch_that_is_not_a_seed_branch(
    tmp_path: Path, bad: str
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    ops = GitOps(allow_file=True, runner=argv_runner(calls))
    with pytest.raises((ValueError, GitOpsError)):
        push_seed_branch(ops, tmp_path / "nope", bad, token=SENTINEL_TOKEN)
    assert calls == []


def local_seed_repo(tmp_path: Path, base: str, branch: str, text: str) -> Path:
    path = tmp_path / "local"
    git("init", "-q", "-b", branch, str(path))
    (path / "a.txt").write_bytes(text.encode())
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", text, cwd=path)
    git("remote", "add", "origin", f"{base}/{ALLOWED_REPO}.git", cwd=path)
    return path


def test_push_seed_branch_pushes_the_branch_and_never_forces(
    tmp_path: Path, remote: tuple[str, Path]
) -> None:
    base, bare = remote
    branch = "seed/01-paginate-off-by-one"
    path = local_seed_repo(tmp_path, base, branch, "one")
    ops = new_git_ops(base)
    push_seed_branch(ops, path, branch, token=SENTINEL_TOKEN)
    pushed = ref_sha(bare, ref(branch))
    assert pushed == git("rev-parse", "HEAD", cwd=path)
    # the same branch rewritten locally is a non-fast-forward: refused, never forced
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=path)
    with pytest.raises(SeedBranchConflict) as err:
        push_seed_branch(ops, path, branch, token=SENTINEL_TOKEN)
    assert SENTINEL_TOKEN not in str(err.value)
    assert ref_sha(bare, ref(branch)) == pushed


def test_push_seed_branch_pushes_nothing_but_the_named_branch(
    tmp_path: Path, remote: tuple[str, Path]
) -> None:
    base, bare = remote
    branch = "seed/02-mutable-default"
    path = local_seed_repo(tmp_path, base, branch, "two")
    git("branch", "agent-extra", cwd=path)
    git("tag", "v1", cwd=path)
    push_seed_branch(new_git_ops(base), path, branch, token=SENTINEL_TOKEN)
    assert refs(bare) == {"refs/heads/main", ref(branch)}


# --- the backend guard stays as it is ------------------------------------------------------


@pytest.mark.parametrize("branch", ["seed/01-a", "seed/01-paginate-off-by-one", "main"])
def test_gitops_push_still_refuses_seed_and_other_non_agent_branches(
    tmp_path: Path, branch: str
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    ops = GitOps(allow_file=True, runner=argv_runner(calls))
    with pytest.raises(GitOpsError, match=r"agent/<task-id>"):
        ops.push(tmp_path, branch, token=SENTINEL_TOKEN)
    assert calls == []


def test_gitops_push_still_has_no_seed_escape_hatch() -> None:
    import inspect

    params: dict[str, Any] = dict(inspect.signature(GitOps.push).parameters)
    assert list(params) == ["self", "path", "branch", "token"]
