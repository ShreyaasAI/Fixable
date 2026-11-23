"""`swf_eval.resolve.build_check_tree` and `check_resolution` plumbing (m9-contract §0.10, §1.5).

No Docker: the check tree is built from the pristine seed plus the agent's non-protected files,
and a fake `Runner` records which tree the commands ran in. The real sandbox runs are in
`test_m9_eval_resolve_docker.py`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from m9_eval_fakes import copy_seed_repo, tree_files, write
from swf_eval.resolve import (
    PROTECTED_NAMES,
    CheckTree,
    build_check_tree,
    check_resolution,
    is_protected,
)
from swf_eval.seeds import Seed, load_seeds

from swf.config import load_agents_file


@pytest.fixture(scope="module")
def seed01() -> Seed:
    return next(s for s in load_seeds() if s.name.startswith("01-"))


@pytest.fixture
def pr_tree(seed01: Seed, tmp_path: Path) -> Path:
    """A copy of seed 01's `repo/`: what a PR head clone looks like before the agent edits it."""
    return copy_seed_repo(seed01.repo_dir, tmp_path / "pr")


# --- is_protected --------------------------------------------------------------------------


def test_the_protected_names_are_the_contract_set() -> None:
    assert (
        frozenset(
            {"AGENTS.md", "pytest.ini", "conftest.py", "setup.cfg", "tox.ini", "pyproject.toml"}
        )
        == PROTECTED_NAMES
    )


@pytest.mark.parametrize(
    "rel",
    [
        "tests/test_pager.py",
        "tests/sub/test_x.py",
        "tests/data/fixture.json",
        "AGENTS.md",
        "pytest.ini",
        "conftest.py",
        "pkg/conftest.py",
        "setup.cfg",
        "tox.ini",
        "pyproject.toml",
        "sub/pyproject.toml",
    ],
)
def test_protected_paths(rel: str) -> None:
    assert is_protected(rel)


@pytest.mark.parametrize(
    "rel",
    ["pager.py", "pkg/pager.py", "pytest.py", "pytest.toml", "sitecustomize.py", "README.md"],
)
def test_unprotected_paths(rel: str) -> None:
    assert not is_protected(rel)


# --- build_check_tree ----------------------------------------------------------------------


def test_the_agents_edit_to_a_source_file_is_in_the_check_tree(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    write(pr_tree, "pager.py", "# agent edit\n" + (pr_tree / "pager.py").read_text("utf-8"))
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert isinstance(tree, CheckTree)
    assert (tree.root / "pager.py").read_text("utf-8").startswith("# agent edit\n")
    assert tree.tests_touched == () and tree.files_added == () and tree.files_deleted == ()


def test_the_check_tree_is_the_pristine_tree_when_the_pr_changed_nothing(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert tree_files(tree.root) == tree_files(seed01.repo_dir)


def test_edited_tests_and_a_deleted_pytest_ini_come_back_pristine(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    pristine = tree_files(seed01.repo_dir)
    write(pr_tree, "tests/test_pager.py", "def test_second_page():\n    assert True\n")
    (pr_tree / "pytest.ini").unlink()
    write(pr_tree, "AGENTS.md", "# the agent rewrote the instructions\n")
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    built = tree_files(tree.root)
    for rel in ("tests/test_pager.py", "pytest.ini", "AGENTS.md"):
        assert built[rel] == pristine[rel], rel
    assert tree.tests_touched == ("AGENTS.md", "pytest.ini", "tests/test_pager.py")
    assert tree.files_added == () and tree.files_deleted == ()


def test_deleting_the_target_test_does_not_remove_it_from_the_check_tree(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    (pr_tree / "tests" / "test_pager.py").unlink()
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert (tree.root / "tests" / "test_pager.py").read_bytes() == (
        seed01.repo_dir / "tests" / "test_pager.py"
    ).read_bytes()
    assert "tests/test_pager.py" in tree.tests_touched


def test_planted_files_never_reach_the_check_tree(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    """pytest.py shadows the real pytest, pytest.toml/conftest.py steer it, sitecustomize.py runs
    at interpreter start: none of them may be in the tree the checker runs."""
    write(pr_tree, "pager.py", "# agent edit\n" + (pr_tree / "pager.py").read_text("utf-8"))
    planted = {
        "tests/test_extra.py": "def test_x():\n    pass\n",
        "conftest.py": "def pytest_collection_modifyitems(items):\n    items.clear()\n",
        "pytest.py": "raise SystemExit(0)\n",
        "pytest.toml": "[pytest]\naddopts = '--co'\n",
        "sitecustomize.py": "import os\nos._exit(0)\n",
        "pkg/helper.py": "X = 1\n",
    }
    for rel, text in planted.items():
        write(pr_tree, rel, text)
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    built = tree_files(tree.root)
    for rel in planted:
        assert rel not in built, rel
    assert not (tree.root / "pkg").exists()
    assert built["pager.py"].startswith(b"# agent edit\n")  # the legitimate edit still lands
    assert tree.files_added == (
        "conftest.py",
        "pkg/helper.py",
        "pytest.py",
        "pytest.toml",
        "sitecustomize.py",
        "tests/test_extra.py",
    )
    # protected-shaped additions are also reported as touched tests; pytest.toml is not protected
    assert tree.tests_touched == ("conftest.py", "tests/test_extra.py")
    assert tree.files_deleted == ()


def test_a_deleted_unprotected_source_file_stays_deleted(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    (pr_tree / "pager.py").unlink()
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert not (tree.root / "pager.py").exists()
    assert tree.files_deleted == ("pager.py",)
    assert tree.tests_touched == ()


def test_a_git_directory_in_the_pr_tree_is_ignored(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    write(pr_tree, ".git/config", "[core]\n\thooksPath = /tmp/x\n")
    write(pr_tree, ".git/hooks/pre-commit", "#!/bin/sh\nexit 0\n")
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert not (tree.root / ".git").exists()
    assert tree.files_added == ()


def test_a_symlink_in_the_pr_tree_is_never_followed_or_copied(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"not for the sandbox")
    try:
        os.symlink(secret, pr_tree / "link.txt")
        os.symlink(tmp_path, pr_tree / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this host")
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert not (tree.root / "link.txt").exists() and not (tree.root / "link.txt").is_symlink()
    assert not (tree.root / "linkdir").exists()
    assert "link.txt" in tree.files_added
    assert b"not for the sandbox" not in b"".join(tree_files(tree.root).values())


def test_a_symlink_replacing_a_source_file_is_not_followed(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"not for the sandbox")
    (pr_tree / "pager.py").unlink()
    try:
        os.symlink(secret, pr_tree / "pager.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this host")
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert b"not for the sandbox" not in b"".join(tree_files(tree.root).values())


def test_an_existing_destination_is_an_error(seed01: Seed, pr_tree: Path, tmp_path: Path) -> None:
    dest = tmp_path / "check"
    dest.mkdir()
    (dest / "keep.txt").write_bytes(b"x")
    with pytest.raises((OSError, ValueError)):
        build_check_tree(seed01, pr_tree, dest)
    assert (dest / "keep.txt").read_bytes() == b"x"


def test_the_pristine_seed_is_never_modified(seed01: Seed, pr_tree: Path, tmp_path: Path) -> None:
    before = tree_files(seed01.repo_dir)
    write(pr_tree, "pager.py", "# agent edit\n")
    write(pr_tree, "pytest.py", "raise SystemExit(0)\n")
    (pr_tree / "pytest.ini").unlink()
    build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert tree_files(seed01.repo_dir) == before


def test_reference_is_never_copied_into_the_check_tree(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert "reference" not in {p.name for p in tree.root.iterdir()}
    assert tree_files(tree.root).get("pager.py") != (seed01.reference_dir / "pager.py").read_bytes()


def test_lists_are_sorted(seed01: Seed, pr_tree: Path, tmp_path: Path) -> None:
    for rel in ("z_last.py", "a_first.py", "tests/b.py", "tests/a.py"):
        write(pr_tree, rel, "x = 1\n")
    tree = build_check_tree(seed01, pr_tree, tmp_path / "check")
    assert list(tree.files_added) == sorted(tree.files_added)
    assert list(tree.tests_touched) == sorted(tree.tests_touched)


# --- check_resolution plumbing (fake runner) -----------------------------------------------


class RecordingRunner:
    """A `Runner`: logs (seed, tree files, commands) and answers the scripted exit codes."""

    def __init__(self, exits: Sequence[int]) -> None:
        self.exits = list(exits)
        self.calls: list[tuple[str, dict[str, bytes], list[str]]] = []

    def __call__(self, seed: Seed, tree: Path, commands: Sequence[str]) -> list[int]:
        self.calls.append((seed.name, tree_files(tree), list(commands)))
        return self.exits[: len(commands)]


def test_check_resolution_runs_setup_then_target_then_the_suite_in_the_check_tree(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    write(pr_tree, "pytest.py", "raise SystemExit(0)\n")
    write(pr_tree, "pager.py", "# agent edit\n" + (pr_tree / "pager.py").read_text("utf-8"))
    runner = RecordingRunner([0, 0, 0])
    res = check_resolution(seed01, pr_tree, tmp_path / "work", runner)
    assert res.resolved and res.setup_ok
    assert (res.target_exit, res.suite_exit) == (0, 0)
    assert res.files_added == ("pytest.py",) and res.tests_touched == ()
    assert len(runner.calls) == 1
    name, files, commands = runner.calls[0]
    assert name == seed01.name
    assert "pytest.py" not in files and files["pager.py"].startswith(b"# agent edit\n")
    setup = load_agents_file(seed01.repo_dir).config.setup
    assert commands == [*setup, f"python -m pytest -q {seed01.target_test}", "python -m pytest -q"]


def test_check_resolution_is_unresolved_when_the_target_still_fails(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    res = check_resolution(seed01, pr_tree, tmp_path / "work", RecordingRunner([0, 1, 1]))
    assert not res.resolved and res.setup_ok and res.target_exit == 1


def test_check_resolution_is_unresolved_when_only_the_suite_fails(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    res = check_resolution(seed01, pr_tree, tmp_path / "work", RecordingRunner([0, 0, 1]))
    assert not res.resolved and res.target_exit == 0 and res.suite_exit == 1


def test_check_resolution_stops_at_a_failed_setup(
    seed01: Seed, pr_tree: Path, tmp_path: Path
) -> None:
    res = check_resolution(seed01, pr_tree, tmp_path / "work", RecordingRunner([1]))
    assert not res.resolved and not res.setup_ok
    assert res.target_exit is None and res.suite_exit is None
