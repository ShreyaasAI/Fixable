"""Builders shared by the M9 eval-harness tests (m9-contract §1.5, §3 T7).

`swf_eval` is imported at module level on purpose: until the package exists every M9 eval test
fails at import (`ModuleNotFoundError: swf_eval`), not on a fixture.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from swf_eval.metrics import TaskRecord
from swf_eval.resolve import Resolution

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_REPO = "ShreyaasAI/swf-pr-sandbox"
SEED_NAMES = [
    "01-paginate-off-by-one",
    "02-mutable-default",
    "03-none-last-name",
    "04-average-int-division",
    "05-top-scores-order",
    "06-word-count-keyerror",
    "07-email-plus-sign",
    "08-iso-date-parse",
    "09-slugify-spaces",
    "10-clamp-upper-bound",
]


def resolution(
    resolved: bool = True,
    *,
    tests_touched: tuple[str, ...] = (),
    files_added: tuple[str, ...] = (),
) -> Resolution:
    return Resolution(
        setup_ok=True,
        target_exit=0 if resolved else 1,
        suite_exit=0,
        tests_touched=tests_touched,
        files_added=files_added,
    )


def rec(
    pass_: str,
    seed: str,
    wall: float | None,
    resolved: bool,
    status: str = "completed",
    **overrides: Any,
) -> TaskRecord:
    """One record; `wall` is `finished_at - claimed_at` (None: no claim, so no wall)."""
    fields: dict[str, Any] = {
        "pass_": pass_,
        "seed": seed,
        "user": "eval-a",
        "task_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{pass_}/{seed}")),
        "status": status,
        "error": None,
        "claimed_at": None if wall is None else 0.0,
        "finished_at": wall,
        "agent_calls": 10,
        "guard_chunks": 5,
        "guard_seconds": 3.0,
        "retries": 0,
        "approvals": {"plan": 1},
        "questions": 0,
        "cost_usd": 0.0,
        "pr_url": f"https://github.com/{ALLOWED_REPO}/pull/1" if status == "completed" else None,
        "resolution": resolution(resolved) if status == "completed" else None,
        "peak_mem_mb": None,
    }
    fields.update(overrides)
    return TaskRecord(**fields)


def full_run(
    single_wall: float, conc_wall: float, single_ok: int, conc_ok: int
) -> list[TaskRecord]:
    """10 seeds x 2 passes; seed index i resolves iff i < the pass's `ok` count."""
    return [rec("single", f"{i:02}", single_wall, i < single_ok) for i in range(10)] + [
        rec("concurrent", f"{i:02}", conc_wall, i < conc_ok) for i in range(10)
    ]


def copy_seed_repo(src: Path, dest: Path) -> Path:
    """A PR-tree stand-in: the seed's pristine `repo/` copied (no bytecode)."""
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def tree_files(root: Path) -> dict[str, bytes]:
    """Every regular file under `root` (posix relpath -> bytes); `.git` and bytecode excluded."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts or not path.is_file():
            continue
        out[rel.as_posix()] = path.read_bytes()
    return out


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
