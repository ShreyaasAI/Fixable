"""DoD 1: hooks never run (m6-contract §0.3, §8). Global, system, template and workspace-planted
hooks, fsmonitor, credential helpers and filters must not fire when the backend clones, commits and
pushes. A control proves the traps do fire for plain git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from m6_fakes import (
    DEFAULT_FILES,
    REPO,
    SENTINEL_TOKEN,
    FakeGitHub,
    bare_remote,
    bare_tree,
    base_sha_of,
    make_patch,
    make_request,
    make_task,
    marker_names,
    new_git_ops,
    new_publisher,
    plant_hook_traps,
    plant_workspace_traps,
    ref_sha,
    trap_pushurl,
)

from swf.gitops import branch_name

FIX = {"pager.py": "def paginate(items, page, size):\n    return items\n"}


def test_the_traps_fire_for_plain_git_without_gitops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with the traps planted, ordinary git (inheriting the environment) trips them."""
    markers = plant_hook_traps(tmp_path, monkeypatch)
    work = tmp_path / "plain"
    work.mkdir()
    env = dict(os.environ)
    subprocess.run(["git", "init", "-q"], cwd=work, env=env, check=True)
    (work / "a.txt").write_text("a", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=work, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=work, env=env, check=True)
    assert {"pre-commit", "post-commit"} <= set(marker_names(markers))


def test_global_template_and_system_traps_never_run_through_clone_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bare = bare_remote(tmp_path, DEFAULT_FILES)
    patch = make_patch(base, tmp_path, FIX)
    markers = plant_hook_traps(tmp_path, monkeypatch)
    task = make_task()
    root = tmp_path / "ws"
    root.mkdir()
    result = new_publisher(base, FakeGitHub()).publish(
        make_request(task, root, patch, base_sha_of(bare)), task
    )
    assert ref_sha(bare, f"refs/heads/{branch_name(task.id)}") == result.commit_sha
    assert marker_names(markers) == []


def test_workspace_planted_traps_never_run_and_the_push_goes_to_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bare = bare_remote(tmp_path, DEFAULT_FILES)
    patch = make_patch(base, tmp_path, FIX)
    markers = plant_hook_traps(tmp_path, monkeypatch)
    task = make_task()
    root = tmp_path / "ws"
    root.mkdir()
    ops = new_git_ops(base)
    ops.clone(REPO, "main", root / "repo", token=SENTINEL_TOKEN)
    plant_workspace_traps(root / "repo", markers)
    assert marker_names(markers) == []
    new_publisher(base, FakeGitHub()).publish(
        make_request(task, root, patch, ops.head(root / "repo")), task
    )
    assert marker_names(markers) == []
    assert ref_sha(bare, f"refs/heads/{branch_name(task.id)}")
    assert not trap_pushurl(markers).exists()


def test_a_patch_that_adds_hooks_and_filter_attributes_publishes_as_plain_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, bare = bare_remote(tmp_path, DEFAULT_FILES)
    edits = {
        **FIX,
        ".githooks/pre-commit": "#!/bin/sh\necho pwned\n",
        ".gitattributes": "* filter=evil\n",
        ".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = file:///nonexistent\n',
    }
    patch = make_patch(base, tmp_path, edits)
    markers = plant_hook_traps(tmp_path, monkeypatch)
    task = make_task()
    root = tmp_path / "ws"
    root.mkdir()
    new_publisher(base, FakeGitHub()).publish(
        make_request(task, root, patch, base_sha_of(bare)), task
    )
    tree = bare_tree(bare, f"refs/heads/{branch_name(task.id)}")
    assert tree[".githooks/pre-commit"] == b"#!/bin/sh\necho pwned\n"
    assert tree[".gitattributes"] == b"* filter=evil\n"
    assert marker_names(markers) == []
