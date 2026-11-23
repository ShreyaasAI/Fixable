"""The resolution checker (m9-contract §0.10, §1.5): pristine seed + the agent's non-protected
files, run in a fresh sandbox. Planted or edited tests and pytest config never reach the run."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from swf.config import load_agents_file
from swf_eval.seeds import IGNORE, Seed

if TYPE_CHECKING:
    from swf.egress import EgressConfig

PROTECTED_NAMES = frozenset(
    {"AGENTS.md", "pytest.ini", "conftest.py", "setup.cfg", "tox.ini", "pyproject.toml"}
)
_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})


def is_protected(rel: str) -> bool:
    """Under a `tests/` directory, or a basename in PROTECTED_NAMES."""
    parts = PurePosixPath(rel).parts
    return "tests" in parts[:-1] or (bool(parts) and parts[-1] in PROTECTED_NAMES)


@dataclass(frozen=True)
class CheckTree:
    root: Path
    tests_touched: tuple[str, ...]
    files_added: tuple[str, ...]
    files_deleted: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    setup_ok: bool
    target_exit: int | None
    suite_exit: int | None
    tests_touched: tuple[str, ...]
    files_added: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return self.setup_ok and self.target_exit == 0 and self.suite_exit == 0


Runner = Callable[[Seed, Path, Sequence[str]], list[int]]


def _walk(root: Path) -> tuple[set[str], set[str]]:
    """(regular files, symlinks) under root as posix relpaths; never follows links."""
    files: set[str] = set()
    links: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        keep = []
        for d in dirnames:
            if (base / d).is_symlink():
                links.add((base / d).relative_to(root).as_posix())
            elif d not in _SKIP_DIRS:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            path = base / f
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                links.add(rel)
            elif f.endswith(".pyc"):
                continue
            elif path.is_file():
                files.add(rel)
    return files, links


def build_check_tree(seed: Seed, pr_tree: Path, dest: Path) -> CheckTree:
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"check tree destination exists: {dest}")
    pristine, _ = _walk(seed.repo_dir)
    pr_files, pr_links = _walk(pr_tree)
    shutil.copytree(seed.repo_dir, dest, ignore=IGNORE)
    touched: set[str] = set()
    deleted: set[str] = set()
    for rel in pristine:
        original = (seed.repo_dir / rel).read_bytes()
        present = rel in pr_files
        if is_protected(rel):
            if not present or (pr_tree / rel).read_bytes() != original:
                touched.add(rel)
        elif present:
            (dest / rel).write_bytes((pr_tree / rel).read_bytes())
        else:
            (dest / rel).unlink()
            deleted.add(rel)
    added = (pr_files - pristine) | pr_links
    touched |= {rel for rel in added if is_protected(rel)}
    return CheckTree(
        root=dest,
        tests_touched=tuple(sorted(touched)),
        files_added=tuple(sorted(added)),
        files_deleted=tuple(sorted(deleted)),
    )


def check_resolution(seed: Seed, pr_tree: Path, work: Path, run: Runner) -> Resolution:
    work.mkdir(parents=True, exist_ok=True)
    tree = build_check_tree(seed, pr_tree, Path(tempfile.mkdtemp(dir=work)) / "repo")
    setup = load_agents_file(seed.repo_dir).config.setup
    commands = [*setup, f"python -m pytest -q {seed.target_test}", "python -m pytest -q"]
    exits = run(seed, tree.root, commands)
    n = len(setup)
    setup_ok = len(exits) >= n and all(code == 0 for code in exits[:n])
    rest = exits[n:] if setup_ok else []
    return Resolution(
        setup_ok=setup_ok,
        target_exit=rest[0] if len(rest) > 0 else None,
        suite_exit=rest[1] if len(rest) > 1 else None,
        tests_touched=tree.tests_touched,
        files_added=tree.files_added,
    )


def sandbox_runner(egress: EgressConfig) -> Runner:
    """A fresh sandbox per call (image and resources from the seed's AGENTS.md)."""

    def run(seed: Seed, tree: Path, commands: Sequence[str]) -> list[int]:
        from swf.sandbox import open_task_sandbox
        from swf.worker import EnvRequest

        cfg = load_agents_file(seed.repo_dir).config
        req = EnvRequest(
            task_id=f"swf-eval-{uuid.uuid4().hex[:12]}",
            image=cfg.image,
            workspace=Path(tree).resolve(),
            memory_mb=cfg.resources.memory_mb,
            cpus=cfg.resources.cpus,
        )
        env = open_task_sandbox(egress, req)
        exits: list[int] = []
        try:
            for command in commands:
                code = int(env.execute({"command": command})["returncode"])
                exits.append(code)
                if code != 0 and command in cfg.setup:
                    break
        finally:
            env.cleanup()
        return exits

    return run


def run_controls(seed: Seed, run: Runner, work: Path) -> tuple[Resolution, Resolution]:
    """(pristine, pristine + reference/): the first must not resolve, the second must."""
    pristine = check_resolution(seed, seed.repo_dir, work / "pristine", run)
    fixed = work / "reference-tree"
    shutil.copytree(seed.repo_dir, fixed, ignore=IGNORE)
    shutil.copytree(seed.reference_dir, fixed, ignore=IGNORE, dirs_exist_ok=True)
    reference = check_resolution(seed, fixed, work / "reference", run)
    return pristine, reference
