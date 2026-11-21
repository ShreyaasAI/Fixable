"""`evidence.pr_patch` (m6-contract §5, §0.5, §0.6): the patch applies to a clean clone of the base
and reproduces the workspace byte for byte; unsupported changes fail loudly."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from m6_fakes import (
    REPO,
    bare_remote,
    clone_plain,
    git,
    rmtree_force,
    tree_bytes,
    write_files,
)

from swf.evidence import UnsupportedChange, copy_tree, pr_patch

BASE_FILES: dict[str, str] = {
    "README.md": "# widgets\n",
    "pager.py": "def paginate(items, page, size):\n    return items[:size]\n",
    "src/pkg/mod.py": "VALUE = 1\nOTHER = 2\n",
    "notes.txt": "no newline at end",
    "empty.txt": "",
    "gone.txt": "bye\n",
    "multi.txt": "".join(f"line {i}\n" for i in range(1, 41)),
}

Edits = dict[str, Any]
CASES: dict[str, Edits] = {
    "modified": {"pager.py": "def paginate(items, page, size):\n    return items\n"},
    "added": {"new.txt": "hello\n"},
    "added_nested": {"a/b/c/deep.py": "X = 1\n", "src/pkg/sub/more.py": "Y = 2\n"},
    "deleted": {"gone.txt": None},
    "deleted_nested_last_file": {"src/pkg/mod.py": None},
    "empty_added": {"blank.txt": ""},
    "empty_removed": {"empty.txt": None},
    "no_newline_added": {"tail.txt": "no newline"},
    "no_newline_removed": {"notes.txt": "no newline at end\n"},
    "no_newline_introduced": {"README.md": "# widgets"},
    "empty_to_content": {"empty.txt": "now has text\n"},
    "content_to_empty": {"gone.txt": ""},
    "two_hunks": {
        "multi.txt": "".join(
            f"line {i}{' CHANGED' if i in (2, 38) else ''}\n" for i in range(1, 41)
        )
    },
    "spaces_and_unicode": {"dir with space/f.txt": "a\n", "déjà.txt": "b\n"},
    "crlf_content": {"win.txt": "a\r\nb\r\n"},
    "everything_at_once": {
        "pager.py": "x\n",
        "new/one.txt": "1\n",
        "gone.txt": None,
        "empty.txt": None,
        "blank.txt": "",
        "notes.txt": "changed",
    },
}


@pytest.fixture
def rig(tmp_path: Path) -> tuple[Path, Path, str]:
    """(base start copy, workspace repo, remote base url)."""
    base_url, _ = bare_remote(tmp_path, BASE_FILES)
    start = tmp_path / "baseline"
    repo = tmp_path / "repo"
    clone_plain(base_url, REPO, repo)
    rmtree_force(repo / ".git")
    shutil.copytree(repo, start)
    return start, repo, base_url


def apply_in_fresh_clone(base_url: str, tmp_path: Path, patch: str) -> Path:
    fresh = tmp_path / "fresh"
    clone_plain(base_url, REPO, fresh)
    patch_file = tmp_path / "x.patch"
    patch_file.write_bytes(patch.encode("utf-8"))
    git("apply", "--check", str(patch_file), cwd=fresh)
    git("apply", str(patch_file), cwd=fresh)
    return fresh


@pytest.mark.parametrize("name", list(CASES))
def test_the_patch_reproduces_the_workspace_in_a_clean_clone(
    name: str, rig: tuple[Path, Path, str], tmp_path: Path
) -> None:
    start, repo, base_url = rig
    write_files(repo, CASES[name])
    patch = pr_patch(start, repo)
    assert patch.startswith("diff --git ")
    fresh = apply_in_fresh_clone(base_url, tmp_path, patch)
    assert tree_bytes(fresh) == tree_bytes(repo)


def test_the_patch_has_git_style_headers(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    write_files(repo, {"pager.py": "x\n", "added.txt": "a\n", "gone.txt": None})
    patch = pr_patch(start, repo)
    assert "diff --git a/pager.py b/pager.py" in patch
    assert "--- a/pager.py\n+++ b/pager.py" in patch
    assert "new file mode 100644" in patch and "--- /dev/null\n+++ b/added.txt" in patch
    assert "deleted file mode 100644" in patch and "--- a/gone.txt\n+++ /dev/null" in patch


def test_an_unchanged_workspace_gives_an_empty_patch(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    assert pr_patch(start, repo) == ""


def test_git_directory_changes_are_not_in_the_patch(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    write_files(repo, {".git/hooks/pre-commit": "#!/bin/sh\n", ".git/config": "[core]\n"})
    write_files(repo, {"pager.py": "x\n"})
    patch = pr_patch(start, repo)
    assert ".git/" not in patch
    assert "pager.py" in patch


def test_patch_paths_are_sorted(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    write_files(repo, {"z.txt": "z\n", "a.txt": "a\n", "m/m.txt": "m\n"})
    heads = [ln for ln in pr_patch(start, repo).splitlines() if ln.startswith("diff --git")]
    assert heads == sorted(heads)


# --- unsupported changes --------------------------------------------------------------------


def test_a_new_binary_file_is_unsupported(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    write_files(repo, {"img/logo.bin": b"\x00\x01\x02\xff"})
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "binary"
    assert exc.value.path == "img/logo.bin"


def test_a_modified_binary_file_is_unsupported(tmp_path: Path) -> None:
    start, repo = tmp_path / "start", tmp_path / "repo"
    write_files(start, {"b.dat": b"\x00old"})
    copy_tree(start, repo)
    write_files(repo, {"b.dat": b"\x00new"})
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "binary"


def test_a_symlink_is_unsupported(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    try:
        os.symlink("pager.py", repo / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "symlink"
    assert exc.value.path == "link.py"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_is_unsupported(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    os.mkfifo(repo / "pipe")
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "special"


@pytest.mark.skipif(sys.platform == "win32", reason="tabs are not valid in Windows file names")
def test_a_tab_in_a_name_is_unsupported(rig: tuple[Path, Path, str]) -> None:
    start, repo, _ = rig
    write_files(repo, {"a\tb.txt": "x\n"})
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "special"


def test_a_nested_dot_git_component_is_unsupported_case_insensitively(
    rig: tuple[Path, Path, str],
) -> None:
    start, repo, _ = rig
    write_files(repo, {"sub/.Git/hooks/pre-commit": "#!/bin/sh\n"})
    with pytest.raises(UnsupportedChange) as exc:
        pr_patch(start, repo)
    assert exc.value.reason == "git_dir"
    assert exc.value.path.startswith("sub/")
