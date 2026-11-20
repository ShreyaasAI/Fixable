"""Host-side workspace snapshots, diffs and unified diff text (m5-contract §2, §0.8, §0.9)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from swf.evidence import (
    SNAPSHOT_IGNORE_DIRS,
    Snapshot,
    copy_tree,
    diff_snapshots,
    take_snapshot,
    unified_diff,
)


def put(root: Path, rel: str, text: str | bytes = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path


def link(path: Path, target: str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this host")


# --- digest ---------------------------------------------------------------------------------


def test_entries_record_the_sha256_of_regular_files(tmp_path: Path) -> None:
    put(tmp_path, "a.txt", "hello")
    snap = take_snapshot(tmp_path)
    assert dict(snap.entries) == {"a.txt": "f:" + hashlib.sha256(b"hello").hexdigest()}
    assert snap.files == 1


def test_the_digest_is_the_sha256_of_the_sorted_path_entry_lines(tmp_path: Path) -> None:
    put(tmp_path, "b/two.txt", "2")
    put(tmp_path, "a.txt", "1")
    snap = take_snapshot(tmp_path)
    one = hashlib.sha256(b"1").hexdigest()
    two = hashlib.sha256(b"2").hexdigest()
    text = f"a.txt\0f:{one}\nb/two.txt\0f:{two}\n"
    assert snap.digest == hashlib.sha256(text.encode()).hexdigest()


def test_the_digest_is_stable_and_independent_of_creation_order(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    for rel in ("z.py", "a/b.py", "m.txt"):
        put(first, rel, rel)
    for rel in ("m.txt", "a/b.py", "z.py"):
        put(second, rel, rel)
    assert take_snapshot(first).digest == take_snapshot(first).digest
    assert take_snapshot(first).digest == take_snapshot(second).digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: put(root, "a.txt", "changed"),
        lambda root: put(root, "new.txt"),
        lambda root: (root / "a.txt").unlink(),
        lambda root: (root / "a.txt").rename(root / "renamed.txt"),
    ],
    ids=["modified", "added", "removed", "renamed"],
)
def test_any_content_or_path_change_changes_the_digest(
    tmp_path: Path, mutate: Callable[[Path], object]
) -> None:
    put(tmp_path, "a.txt", "same")
    before = take_snapshot(tmp_path).digest
    mutate(tmp_path)
    assert take_snapshot(tmp_path).digest != before


def test_mode_bits_are_not_compared(tmp_path: Path) -> None:
    path = put(tmp_path, "run.sh", "echo")
    before = take_snapshot(tmp_path).digest
    path.chmod(0o755)
    assert take_snapshot(tmp_path).digest == before


# --- ignore set -----------------------------------------------------------------------------


def test_the_ignore_set_names_the_cache_directories() -> None:
    assert {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} <= set(
        SNAPSHOT_IGNORE_DIRS
    )


def test_ignored_paths_do_not_appear_and_do_not_change_the_digest(tmp_path: Path) -> None:
    put(tmp_path, "pkg/mod.py", "code")
    before = take_snapshot(tmp_path)
    for rel in (
        ".git/index",
        ".git/objects/ab/cd",
        "__pycache__/mod.cpython-312.pyc",
        "pkg/__pycache__/mod.cpython-312.pyc",
        "pkg/deep/er/__pycache__/x.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/3.12/x.json",
        ".ruff_cache/0.6/x",
        "loose.pyc",
        "pkg/loose.pyo",
    ):
        put(tmp_path, rel, "junk")
    after = take_snapshot(tmp_path)
    assert after.digest == before.digest
    assert dict(after.entries) == dict(before.entries)


def test_similar_looking_names_are_not_ignored(tmp_path: Path) -> None:
    put(tmp_path, "notes.pyc.txt")
    put(tmp_path, "pycache/x.py")
    put(tmp_path, "git/config")
    assert set(take_snapshot(tmp_path).entries) == {"notes.pyc.txt", "pycache/x.py", "git/config"}


# --- symlinks -------------------------------------------------------------------------------


def test_a_symlink_is_recorded_as_a_link_and_its_target_is_never_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "secret.txt"
    put(tmp_path, "outside/secret.txt", "secret one")
    repo = tmp_path / "repo"
    repo.mkdir()
    link(repo / "ln", outside)
    first = take_snapshot(repo)
    assert first.entries["ln"] == f"l:{os.readlink(repo / 'ln')}"  # the link text, verbatim
    assert first.files == 1
    put(tmp_path, "outside/secret.txt", "secret two")  # the target changes, the link does not
    assert take_snapshot(repo).digest == first.digest


def test_a_symlinked_directory_is_not_followed(tmp_path: Path) -> None:
    put(tmp_path, "elsewhere/inner/file.txt", "inner")
    repo = tmp_path / "repo"
    repo.mkdir()
    link(repo / "dirlink", tmp_path / "elsewhere")
    assert set(take_snapshot(repo).entries) == {"dirlink"}


def test_a_dangling_symlink_is_recorded(tmp_path: Path) -> None:
    link(tmp_path / "dangling", "nowhere/at/all")
    assert take_snapshot(tmp_path).entries["dangling"] == "l:nowhere/at/all"


# --- diff_snapshots -------------------------------------------------------------------------


def test_diff_snapshots_reports_added_removed_and_modified_sorted(tmp_path: Path) -> None:
    for rel in ("keep.py", "gone_b.py", "gone_a.py", "edit_b.py", "edit_a.py"):
        put(tmp_path, rel, "v1")
    before = take_snapshot(tmp_path)
    (tmp_path / "gone_b.py").unlink()
    (tmp_path / "gone_a.py").unlink()
    put(tmp_path, "edit_b.py", "v2")
    put(tmp_path, "edit_a.py", "v2")
    put(tmp_path, "sub/new_b.py")
    put(tmp_path, "new_a.py")
    diff = diff_snapshots(before, take_snapshot(tmp_path))
    assert diff.added == ("new_a.py", "sub/new_b.py")
    assert diff.removed == ("gone_a.py", "gone_b.py")
    assert diff.modified == ("edit_a.py", "edit_b.py")
    assert not diff.is_empty()


def test_diff_of_identical_snapshots_is_empty(tmp_path: Path) -> None:
    put(tmp_path, "a.py")
    snap = take_snapshot(tmp_path)
    diff = diff_snapshots(snap, take_snapshot(tmp_path))
    assert diff.is_empty()
    assert diff.as_payload()["counts"] == {"added": 0, "removed": 0, "modified": 0}


def test_a_retargeted_symlink_counts_as_modified(tmp_path: Path) -> None:
    link(tmp_path / "ln", "one")
    before = take_snapshot(tmp_path)
    (tmp_path / "ln").unlink()
    link(tmp_path / "ln", "two")
    assert diff_snapshots(before, take_snapshot(tmp_path)).modified == ("ln",)


def test_as_payload_limits_each_list_but_counts_everything(tmp_path: Path) -> None:
    before = take_snapshot(tmp_path)
    for i in range(5):
        put(tmp_path, f"f{i}.txt")
    payload = diff_snapshots(before, take_snapshot(tmp_path)).as_payload(limit=2)
    assert payload["added"] == ["f0.txt", "f1.txt"]
    assert payload["removed"] == []
    assert payload["modified"] == []
    assert payload["counts"] == {"added": 5, "removed": 0, "modified": 0}


def test_as_payload_default_limit_is_100(tmp_path: Path) -> None:
    before = take_snapshot(tmp_path)
    for i in range(120):
        put(tmp_path, f"f{i:03}.txt")
    payload = diff_snapshots(before, take_snapshot(tmp_path)).as_payload()
    assert len(payload["added"]) == 100
    assert payload["counts"]["added"] == 120


def test_a_snapshot_built_from_entries_exposes_digest_and_file_count() -> None:
    snap = Snapshot(entries={"a": "f:" + "0" * 64})
    assert snap.files == 1
    assert len(snap.digest) == 64


# --- copy_tree ------------------------------------------------------------------------------


def test_copy_tree_replaces_the_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    put(src, "a.py", "new")
    put(dst, "stale.py", "old")
    copy_tree(src, dst)
    assert sorted(p.name for p in dst.iterdir()) == ["a.py"]
    assert (dst / "a.py").read_text(encoding="utf-8") == "new"


def test_copy_tree_skips_the_ignore_set_and_keeps_links_as_links(tmp_path: Path) -> None:
    src = tmp_path / "src"
    put(src, "pkg/mod.py", "code")
    put(src, "pkg/__pycache__/mod.pyc", "junk")
    put(src, ".git/index", "junk")
    put(src, "top.pyc", "junk")
    link(src / "ln", "pkg/mod.py")
    dst = tmp_path / "dst"
    copy_tree(src, dst)
    assert take_snapshot(dst).digest == take_snapshot(src).digest
    assert not (dst / "pkg" / "__pycache__").exists()
    assert not (dst / ".git").exists()
    assert not (dst / "top.pyc").exists()
    assert (dst / "ln").is_symlink()
    assert os.readlink(dst / "ln") == "pkg/mod.py"


# --- unified_diff ---------------------------------------------------------------------------


def test_unified_diff_is_empty_when_nothing_changed(tmp_path: Path) -> None:
    put(tmp_path / "repo", "a.py", "one\n")
    copy_tree(tmp_path / "repo", tmp_path / "base")
    assert unified_diff(tmp_path / "base", tmp_path / "repo") == ""


def test_unified_diff_of_a_modified_file_has_git_style_headers_and_hunks(tmp_path: Path) -> None:
    put(tmp_path / "repo", "m.py", "keep\nold line\nkeep2\n")
    copy_tree(tmp_path / "repo", tmp_path / "base")
    put(tmp_path / "repo", "m.py", "keep\nnew line\nkeep2\n")
    lines = unified_diff(tmp_path / "base", tmp_path / "repo").splitlines()
    assert "--- a/m.py" in lines
    assert "+++ b/m.py" in lines
    assert lines.index("--- a/m.py") + 1 == lines.index("+++ b/m.py")
    assert "-old line" in lines
    assert "+new line" in lines
    assert any(line.startswith("@@") for line in lines)


def test_unified_diff_of_an_added_file_uses_dev_null(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    copy_tree(tmp_path / "repo", tmp_path / "base")
    put(tmp_path / "repo", "sub/new.py", "line one\nline two\n")
    lines = unified_diff(tmp_path / "base", tmp_path / "repo").splitlines()
    assert "--- /dev/null" in lines
    assert "+++ b/sub/new.py" in lines
    assert "+line one" in lines


def test_unified_diff_of_a_removed_file_uses_dev_null(tmp_path: Path) -> None:
    put(tmp_path / "repo", "old.py", "bye\n")
    copy_tree(tmp_path / "repo", tmp_path / "base")
    (tmp_path / "repo" / "old.py").unlink()
    lines = unified_diff(tmp_path / "base", tmp_path / "repo").splitlines()
    assert "--- a/old.py" in lines
    assert "+++ /dev/null" in lines
    assert "-bye" in lines


def test_unified_diff_lists_changed_paths_in_sorted_order(tmp_path: Path) -> None:
    for rel in ("b.py", "a.py", "c.py"):
        put(tmp_path / "repo", rel, "v1\n")
    copy_tree(tmp_path / "repo", tmp_path / "base")
    for rel in ("c.py", "a.py", "b.py"):
        put(tmp_path / "repo", rel, "v2\n")
    text = unified_diff(tmp_path / "base", tmp_path / "repo")
    order = [line for line in text.splitlines() if line.startswith("+++ ")]
    assert order == ["+++ b/a.py", "+++ b/b.py", "+++ b/c.py"]


@pytest.mark.parametrize(
    ("old", "new"),
    [(b"PK\x00\x01\x02", b"PK\x00\x01\x03"), (b"\xff\xfe\x00bad", b"\xff\xfe\x00worse")],
    ids=["nul-byte", "not-utf8"],
)
def test_unified_diff_does_not_print_binary_content(tmp_path: Path, old: bytes, new: bytes) -> None:
    put(tmp_path / "repo", "data.bin", old)
    copy_tree(tmp_path / "repo", tmp_path / "base")
    put(tmp_path / "repo", "data.bin", new)
    text = unified_diff(tmp_path / "base", tmp_path / "repo")
    assert "Binary files a/data.bin and b/data.bin differ" in text
    assert "@@" not in text


def test_unified_diff_describes_symlinks(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    copy_tree(tmp_path / "repo", tmp_path / "base")
    link(tmp_path / "repo" / "ln", "pkg/target.py")
    text = unified_diff(tmp_path / "base", tmp_path / "repo")
    assert "Symlink ln -> pkg/target.py" in text
