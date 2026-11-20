"""Evidence recorded by the harness: baseline, agent test runs, diff, re-run, backstop
(m5-contract §2 and §12 A1).

The harness never trusts agent text. It runs the baseline itself (target + suite) before the
agent starts, records the agent's real `run_tests` results in execute/verify pinned to a
workspace digest, refuses `complete_task` until the bundle is complete for the *current*
workspace, and re-runs the target in `confirming`. Snapshots are taken host-side over the
task's workspace (never following symlinks), so the agent cannot tamper with the hashing.

Imports `swf.agent.channel` and `swf.agent.tools`; nothing in `swf.agent` imports this module.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from minisweagent.exceptions import InterruptAgentFlow

from swf import store
from swf.agent.channel import BaselineNotReproduced, NoTestCommand, TaskChannel
from swf.agent.tools import ToolEnv, build_test_command
from swf.db import Database

__all__ = [
    "HARNESS_PREAMBLE",
    "HARNESS_TEST_TIMEOUT_SECONDS",
    "REFUSAL_PREFIX",
    "SNAPSHOT_IGNORE_DIRS",
    "SNAPSHOT_IGNORE_SUFFIXES",
    "BundleProblem",
    "Confirmation",
    "EvidenceKind",
    "EvidenceRecord",
    "EvidenceSink",
    "EvidenceSource",
    "HarnessInfraError",
    "PgEvidenceSink",
    "Snapshot",
    "SnapshotDiff",
    "TaskHarness",
    "UnsupportedChange",
    "bundle_problems",
    "copy_tree",
    "covers_target",
    "diff_snapshots",
    "harness_command",
    "parse_failures",
    "pr_patch",
    "take_snapshot",
    "unified_diff",
]

SNAPSHOT_IGNORE_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
SNAPSHOT_IGNORE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")
HARNESS_TEST_TIMEOUT_SECONDS = 600
HARNESS_PREAMBLE = (
    "export PYTHONDONTWRITEBYTECODE=1; "
    "find /repo -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; "
    "rm -rf /repo/.pytest_cache; "
)
REFUSAL_PREFIX = "complete_task refused: the evidence bundle is incomplete."
EvidenceKind = Literal["baseline", "baseline_suite", "post", "suite", "harness_rerun", "diff"]
EvidenceSource = Literal["harness", "agent_run"]

_BINARY_SNIFF_BYTES = 8192
_PROBLEM_ORDER = (
    "baseline_missing",
    "baseline_not_failing",
    "post_missing",
    "post_failing",
    "suite_missing",
    "suite_new_failures",
    "suite_unparsed",
    "diff_empty",
)
_LISTED_FAILURES = 20
_RERUN_TAIL = 2000
_READONLY_DIFF_LIMIT = 4000
_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR) (\S.*?)(?: - .*)?\s*$")


# --- snapshots -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    entries: Mapping[str, str]  # posix relpath -> "f:<sha256 hex>" | "l:<link target>" | "o"

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.entries):
            h.update(f"{path}\0{self.entries[path]}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    @property
    def files(self) -> int:
        return len(self.entries)


_Kind = Literal["dir", "file", "link", "other"]


def _walk(root: Path) -> Iterator[tuple[str, _Kind, Path]]:
    """Every non-ignored entry under `root` (sorted, top-down); symlinks are never followed."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        rel_base = base.relative_to(root)
        keep: list[str] = []
        for name in sorted(dirnames):
            if name in SNAPSHOT_IGNORE_DIRS:
                continue
            path = base / name
            rel = (rel_base / name).as_posix()
            if path.is_symlink():
                yield rel, "link", path
            else:
                keep.append(name)
                yield rel, "dir", path
        dirnames[:] = keep
        for name in sorted(filenames):
            if name.endswith(SNAPSHOT_IGNORE_SUFFIXES):
                continue
            path = base / name
            rel = (rel_base / name).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                yield rel, "link", path
            elif stat.S_ISREG(mode):
                yield rel, "file", path
            else:
                yield rel, "other", path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def take_snapshot(root: Path) -> Snapshot:
    """Hash every file under `root`; links record their target (never read); §0.9 ignores."""
    entries: dict[str, str] = {}
    for rel, kind, path in _walk(Path(root)):
        if kind == "dir":
            continue
        if kind == "link":
            entries[rel] = "l:" + os.readlink(path)
        elif kind == "file":
            try:
                entries[rel] = "f:" + _sha256_file(path)
            except OSError:
                entries[rel] = "o"  # unreadable: recorded, so it still shows up as a change
        else:
            entries[rel] = "o"
    return Snapshot(entries=entries)


@dataclass(frozen=True)
class SnapshotDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def paths(self) -> list[str]:
        return sorted({*self.added, *self.removed, *self.modified})

    def as_payload(self, limit: int = 100) -> dict[str, Any]:
        return {
            "added": list(self.added[:limit]),
            "removed": list(self.removed[:limit]),
            "modified": list(self.modified[:limit]),
            "counts": {
                "added": len(self.added),
                "removed": len(self.removed),
                "modified": len(self.modified),
            },
        }


def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff:
    old, new = before.entries, after.entries
    return SnapshotDiff(
        added=tuple(sorted(p for p in new if p not in old)),
        removed=tuple(sorted(p for p in old if p not in new)),
        modified=tuple(sorted(p for p in new if p in old and new[p] != old[p])),
    )


def copy_tree(src: Path, dst: Path) -> None:
    """Replace `dst` with a copy of `src` (same ignores; symlinks copied as links)."""
    src, dst = Path(src), Path(dst)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for rel, kind, path in _walk(src):
        target = dst / rel
        if kind == "dir":
            target.mkdir()
        elif kind == "link":
            os.symlink(os.readlink(path), target, target_is_directory=path.is_dir())
        elif kind == "file":
            shutil.copy2(path, target, follow_symlinks=False)
        # "other" (fifos, sockets, devices) is not copied; the snapshot still records it.


# --- diff ----------------------------------------------------------------------------------


def _entry_kind(path: Path) -> Literal["missing", "link", "file", "other"]:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _text(path: Path) -> str | None:
    """The file as UTF-8 text, or None if it looks binary (NUL in the first 8 KiB, not UTF-8)."""
    data = path.read_bytes()
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _file_diff(rel: str, old: Path | None, new: Path | None) -> str:
    old_text = "" if old is None else _text(old)
    new_text = "" if new is None else _text(new)
    if old_text is None or new_text is None:
        return f"Binary files a/{rel} and b/{rel} differ\n"
    fromfile = "/dev/null" if old is None else f"a/{rel}"
    tofile = "/dev/null" if new is None else f"b/{rel}"
    out: list[str] = []
    for line in difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    ):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    if not out:  # an added or removed empty file
        out = [f"--- {fromfile}\n", f"+++ {tofile}\n"]
    return "".join(out)


def unified_diff(base: Path, repo: Path) -> str:
    """Git-style unified diff from `base` to `repo` over the changed paths (sorted)."""
    base, repo = Path(base), Path(repo)
    changed = diff_snapshots(take_snapshot(base), take_snapshot(repo)).paths()
    parts: list[str] = []
    for rel in changed:
        old_path, new_path = base / rel, repo / rel
        old_kind, new_kind = _entry_kind(old_path), _entry_kind(new_path)
        if new_kind == "link":
            parts.append(f"Symlink {rel} -> {os.readlink(new_path)}\n")
        elif old_kind == "link" and new_kind == "missing":
            parts.append(f"Symlink {rel} removed (was -> {os.readlink(old_path)})\n")
        if "other" in (old_kind, new_kind):
            parts.append(f"Special file {rel} changed\n")
            continue
        old_file = old_path if old_kind == "file" else None
        new_file = new_path if new_kind == "file" else None
        if old_file is None and new_file is None:
            continue
        parts.append(_file_diff(rel, old_file, new_file))
    return "".join(parts)


class UnsupportedChange(Exception):
    """A change `pr_patch` cannot carry into a PR (m6 §0.6); the PR fails visibly."""

    def __init__(
        self, path: str, reason: Literal["binary", "symlink", "special", "git_dir"]
    ) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _pr_file_patch(rel: str, old: Path | None, new: Path | None) -> str:
    old_text = "" if old is None else _text(old)
    new_text = "" if new is None else _text(new)
    if old_text is None or new_text is None:
        raise UnsupportedChange(rel, "binary")
    out = [f"diff --git a/{rel} b/{rel}\n"]
    if old is None:
        out.append("new file mode 100644\n")
    elif new is None:
        out.append("deleted file mode 100644\n")
    hunks = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{rel}",
        tofile="/dev/null" if new is None else f"b/{rel}",
    )
    for line in hunks:  # an empty added/removed file has no hunks: header only
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def pr_patch(base: Path, repo: Path) -> str:
    """A `git apply`-able patch from `base` to `repo` (m6 §5); "" if nothing changed.

    Raises `UnsupportedChange` for binary files, symlinks, special files, `.git` components
    and paths with a tab or newline: these are never silently dropped.
    """
    base, repo = Path(base), Path(repo)
    changed = diff_snapshots(take_snapshot(base), take_snapshot(repo)).paths()
    plan: list[tuple[str, Path | None, Path | None]] = []
    for rel in changed:
        if any(part.lower() == ".git" for part in rel.split("/")):
            raise UnsupportedChange(rel, "git_dir")
        if "\t" in rel or "\n" in rel or "\r" in rel:
            raise UnsupportedChange(rel, "special")
        old_path, new_path = base / rel, repo / rel
        kinds = (_entry_kind(old_path), _entry_kind(new_path))
        if "link" in kinds:
            raise UnsupportedChange(rel, "symlink")
        if "other" in kinds:
            raise UnsupportedChange(rel, "special")
        old_file = old_path if kinds[0] == "file" else None
        new_file = new_path if kinds[1] == "file" else None
        if old_file is None and new_file is None:
            continue
        plan.append((rel, old_file, new_file))
    return "".join(_pr_file_patch(rel, old, new) for rel, old, new in plan)


# --- rules ---------------------------------------------------------------------------------


def parse_failures(output: str) -> frozenset[str]:
    """Test ids from pytest short-summary lines (`FAILED <id>` / `ERROR <id>`, §0.11)."""
    found: set[str] = set()
    for line in output.splitlines():
        match = _FAILURE_RE.match(line.rstrip("\r"))
        if match:
            found.add(match.group(1).strip())
    return frozenset(found)


def covers_target(selector: str | None, failing_test: str | None) -> bool:
    if failing_test is None:
        return selector is None
    return selector is None or failing_test in selector


def harness_command(test_command: str | None, selector: str | None) -> str:
    if not test_command:
        raise NoTestCommand()
    return HARNESS_PREAMBLE + build_test_command(test_command, selector)


@dataclass(frozen=True)
class EvidenceRecord:
    kind: EvidenceKind
    source: EvidenceSource
    command: str | None = None
    selector: str | None = None
    exit_code: int | None = None
    output: str | None = None
    output_truncated: bool = False
    output_ref: str | None = None
    failures: frozenset[str] = frozenset()
    duration_ms: int | None = None
    workspace_digest: str | None = None
    tool_call_id: str | None = None
    covers_target: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


ProblemCode = Literal[
    "baseline_missing",
    "baseline_not_failing",
    "post_missing",
    "post_failing",
    "suite_missing",
    "suite_new_failures",
    "suite_unparsed",
    "diff_empty",
]


@dataclass(frozen=True)
class BundleProblem:
    code: ProblemCode
    message: str


def _latest(
    runs: Sequence[EvidenceRecord], pick: Callable[[EvidenceRecord], bool]
) -> EvidenceRecord | None:
    for run in reversed(runs):
        if pick(run):
            return run
    return None


def _hits_target(failure: str, target: str) -> bool:
    return failure == target or failure.startswith((target + "::", target + "["))


def _post_passes(
    post: EvidenceRecord, baseline: EvidenceRecord | None, baseline_suite: EvidenceRecord | None
) -> bool:
    """A covering run passes iff it exits 0, or its failures were parsed, none is the target,
    and all were already failing in the baseline suite (coordinator amendment, 2026-11-20).
    The target is the baseline's selector (None = the whole suite is the target)."""
    if post.exit_code == 0:
        return True
    if not post.failures:
        return False  # non-zero and unparsed
    known = baseline_suite.failures if baseline_suite is not None else frozenset()
    if not post.failures <= known:
        return False
    target = baseline.selector if baseline is not None else None
    if target is None:
        return True
    return not any(_hits_target(f, target) for f in post.failures)


def bundle_problems(
    *,
    baseline: EvidenceRecord | None,
    baseline_suite: EvidenceRecord | None,
    runs: Sequence[EvidenceRecord],
    current_digest: str,
    start_digest: str,
) -> list[BundleProblem]:
    """[] = the bundle is complete for the current workspace; else the problems in code order."""
    problems: list[BundleProblem] = []
    if baseline is None:
        problems.append(
            BundleProblem("baseline_missing", "the harness has no baseline run of the target test")
        )
    elif baseline.exit_code == 0:
        problems.append(
            BundleProblem("baseline_not_failing", "the target test passed before any change")
        )
    current = [r for r in runs if r.workspace_digest == current_digest]
    post = _latest(current, lambda r: r.covers_target)
    if post is None:
        problems.append(
            BundleProblem(
                "post_missing",
                "no run_tests of the target test on the current workspace; run it after your "
                "last change",
            )
        )
    elif not _post_passes(post, baseline, baseline_suite):
        problems.append(
            BundleProblem(
                "post_failing",
                f"the latest run_tests of the target test exited {post.exit_code}; it must pass",
            )
        )
    suite = _latest(current, lambda r: r.selector is None)
    if suite is None:
        problems.append(
            BundleProblem(
                "suite_missing",
                "no run_tests of the full suite (no selector) on the current workspace",
            )
        )
    elif suite.exit_code != 0:
        if not suite.failures:
            problems.append(
                BundleProblem(
                    "suite_unparsed",
                    f"the latest full-suite run exited {suite.exit_code} and its failures could "
                    "not be read from the output",
                )
            )
        else:
            known = baseline_suite.failures if baseline_suite is not None else frozenset()
            new = sorted(suite.failures - known)
            if new:
                listed = ", ".join(new[:_LISTED_FAILURES])
                more = (
                    f" (+{len(new) - _LISTED_FAILURES} more)" if len(new) > _LISTED_FAILURES else ""
                )
                problems.append(
                    BundleProblem(
                        "suite_new_failures",
                        f"the full suite has failures that the baseline did not: {listed}{more}",
                    )
                )
    if current_digest == start_digest:
        problems.append(
            BundleProblem("diff_empty", "the workspace is unchanged since the baseline")
        )
    problems.sort(key=lambda p: _PROBLEM_ORDER.index(p.code))
    return problems


def _problem_lines(problems: Sequence[BundleProblem]) -> str:
    return "".join(f"\n- [{p.code}] {p.message}" for p in problems)


# --- sink ----------------------------------------------------------------------------------


class EvidenceSink(Protocol):
    def add(self, record: EvidenceRecord) -> int: ...


class PgEvidenceSink:
    """`EvidenceSink` over `store.add_evidence`, one backend transaction per call."""

    def __init__(
        self,
        db: Database,
        task_id: uuid.UUID,
        *,
        is_stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        self.db = db
        self.task_id = task_id
        self._is_stopped = is_stopped

    def add(self, record: EvidenceRecord) -> int:
        if self._is_stopped():
            return -1
        details = {**record.details, "covers_target": record.covers_target}
        with self.db.backend() as conn:
            row = store.add_evidence(
                conn,
                self.task_id,
                kind=record.kind,
                source=record.source,
                command=record.command,
                selector=record.selector,
                exit_code=record.exit_code,
                output=record.output,
                output_truncated=record.output_truncated,
                output_ref=record.output_ref,
                failures=sorted(record.failures),
                duration_ms=record.duration_ms,
                workspace_digest=record.workspace_digest,
                tool_call_id=record.tool_call_id,
                details=details,
            )
        return row.id


# --- the harness ---------------------------------------------------------------------------


class HarnessInfraError(Exception):
    """A harness test run did not run (rc -1 / exception_info / the environment raised)."""


@dataclass(frozen=True)
class Confirmation:
    passed: bool
    exit_code: int | None
    evidence_id: int | None
    discrepancy: dict[str, Any] | None


@dataclass(frozen=True)
class _Run:
    command: str
    output: str
    exit_code: int
    duration_ms: int


def _is_infra(out: Mapping[str, Any]) -> bool:
    return out.get("returncode") == -1 or bool(out.get("exception_info"))


class TaskHarness:
    """Implements `web_agent.EvidenceHarness` (and so `EvidenceGate`) for one task."""

    def __init__(
        self,
        *,
        env: ToolEnv,
        channel: TaskChannel,
        sink: EvidenceSink,
        repo: Path,
        root: Path,
        test_command: str | None,
        failing_test: str | None,
        output_limit: int = 16000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.env = env
        self.channel = channel
        self.sink = sink
        self.repo = Path(repo)
        self.root = Path(root)
        self.test_command = test_command
        self.failing_test = failing_test
        self.output_limit = output_limit
        self.clock = clock
        self.started = False
        self.start_digest: str | None = None
        self.checked_digest: str | None = None
        self.baseline: EvidenceRecord | None = None
        self.baseline_suite: EvidenceRecord | None = None
        self.rerun: EvidenceRecord | None = None
        self.runs: list[EvidenceRecord] = []
        self._run_ids: list[int] = []
        self._start: Snapshot | None = None
        self._post_evidence_id: int | None = None
        self._files = 0

    # --- internals -------------------------------------------------------------------------

    @property
    def baseline_dir(self) -> Path:
        return self.root / "baseline"

    def _execute(self, command: str) -> _Run:
        started = self.clock()
        try:
            out = self.env.execute({"command": command}, timeout=HARNESS_TEST_TIMEOUT_SECONDS)
        except InterruptAgentFlow:
            raise
        except Exception as exc:
            raise HarnessInfraError(f"{type(exc).__name__}: {exc}") from exc
        if _is_infra(out):
            detail = str(out.get("exception_info") or out.get("output") or "")[-500:]
            raise HarnessInfraError(f"harness test run did not run: {detail}")
        duration_ms = max(0, int((self.clock() - started) * 1000))
        return _Run(
            command=command,
            output=str(out.get("output") or ""),
            exit_code=int(out.get("returncode", 1)),
            duration_ms=duration_ms,
        )

    def _save(self, kind: str, text: str, *, head: bool = False) -> tuple[str, bool, str]:
        """Write the full text under `root/evidence/`; returns (shown, truncated, output_ref)."""
        self._files += 1
        suffix = ".patch" if kind == "diff" else ".txt"
        path = self.root / "evidence" / f"{self._files:03d}-{kind}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="surrogateescape", newline="\n")
        limit = self.output_limit
        truncated = len(text) > limit
        shown = text if not truncated else (text[:limit] if head else text[-limit:])
        return shown, truncated, path.relative_to(self.root).as_posix()

    def _test_record(
        self,
        kind: EvidenceKind,
        source: EvidenceSource,
        run: _Run,
        *,
        selector: str | None,
        digest: str,
        covers: bool,
        tool_call_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        saved: tuple[str, bool, str] | None = None,
    ) -> EvidenceRecord:
        shown, truncated, ref = saved if saved is not None else self._save(kind, run.output)
        return EvidenceRecord(
            kind=kind,
            source=source,
            command=run.command,
            selector=selector,
            exit_code=run.exit_code,
            output=shown,
            output_truncated=truncated,
            output_ref=ref,
            failures=parse_failures(run.output),
            duration_ms=run.duration_ms,
            workspace_digest=digest,
            tool_call_id=tool_call_id,
            covers_target=covers,
            details=dict(details or {}),
        )

    def _problems(self) -> tuple[list[BundleProblem], Snapshot]:
        snapshot = take_snapshot(self.repo)
        problems = bundle_problems(
            baseline=self.baseline,
            baseline_suite=self.baseline_suite,
            runs=self.runs,
            current_digest=snapshot.digest,
            start_digest=self.start_digest or "",
        )
        return problems, snapshot

    # --- preparing -------------------------------------------------------------------------

    def run_baseline(self) -> None:
        self._baseline(rebaseline=False)

    def _baseline(self, *, rebaseline: bool) -> None:
        tc = self.test_command
        if not tc:
            raise NoTestCommand()
        target = self._execute(harness_command(tc, self.failing_test))
        suite = self._execute(harness_command(tc, None)) if self.failing_test else None
        # Rows only after every run succeeded (an infra failure writes nothing).
        digest = take_snapshot(self.repo).digest
        extra: dict[str, Any] = {"rebaseline": True} if rebaseline else {}
        if suite is None:
            shared = {**extra, "shared_run": True}
            saved = self._save("baseline", target.output)
            baseline = self._test_record(
                "baseline",
                "harness",
                target,
                selector=None,
                digest=digest,
                covers=True,
                details=shared,
                saved=saved,
            )
            baseline_suite = self._test_record(
                "baseline_suite",
                "harness",
                target,
                selector=None,
                digest=digest,
                covers=True,
                details=shared,
                saved=saved,
            )
        else:
            baseline = self._test_record(
                "baseline",
                "harness",
                target,
                selector=self.failing_test,
                digest=digest,
                covers=True,
                details=extra,
            )
            baseline_suite = self._test_record(
                "baseline_suite",
                "harness",
                suite,
                selector=None,
                digest=digest,
                covers=covers_target(None, self.failing_test),
                details=extra,
            )
        self.sink.add(baseline)
        self.sink.add(baseline_suite)
        self.baseline, self.baseline_suite = baseline, baseline_suite
        if target.exit_code == 0:
            self.channel.emit("baseline_not_reproduced", {"exit_code": target.exit_code})
            raise BaselineNotReproduced()

    def take_start_snapshot(self) -> Snapshot:
        snapshot = take_snapshot(self.repo)
        copy_tree(self.repo, self.baseline_dir)
        self._start = snapshot
        self.start_digest = snapshot.digest
        self.started = True
        self.channel.emit("backstop_snapshot", {"digest": snapshot.digest, "files": snapshot.files})
        return snapshot

    # --- investigate (backstop) ------------------------------------------------------------

    def check_readonly(self) -> dict[str, Any]:
        if not self.started or self._start is None:
            raise RuntimeError("check_readonly before take_start_snapshot")
        return diff_snapshots(self._start, take_snapshot(self.repo)).as_payload()

    def readonly_diff(self) -> tuple[str, bool]:
        """Head of the diff from the start copy to the workspace, and whether it was cut."""
        text = unified_diff(self.baseline_dir, self.repo)
        return text[:_READONLY_DIFF_LIMIT], len(text) > _READONLY_DIFF_LIMIT

    def rebaseline(self, approval_id: Any = None) -> None:
        """The user accepted an investigate-phase change: it becomes part of the base (A1)."""
        self._baseline(rebaseline=True)
        snapshot = self.take_start_snapshot()
        self.channel.emit(
            "backstop_rebaseline",
            {
                "digest": snapshot.digest,
                "files": snapshot.files,
                "approval_id": None if approval_id is None else str(approval_id),
            },
        )

    # --- execute / verify ------------------------------------------------------------------

    def record_run(
        self, *, tool_call_id: str, selector: str | None, output: dict[str, Any], phase: str
    ) -> None:
        if phase not in ("execute", "verify") or _is_infra(output):
            return
        covers = covers_target(selector, self.failing_test)
        if not (covers or selector is None):
            return
        kind: EvidenceKind = "suite" if selector is None else "post"
        run = _Run(
            command=build_test_command(self.test_command, selector),
            output=str(output.get("output") or ""),
            exit_code=int(output.get("returncode", 1)),
            duration_ms=0,
        )
        record = self._test_record(
            kind,
            "agent_run",
            run,
            selector=selector,
            digest=take_snapshot(self.repo).digest,
            covers=covers,
            tool_call_id=tool_call_id,
        )
        record = dataclasses.replace(record, duration_ms=None)  # the agent's run is not timed
        self._run_ids.append(self.sink.add(record))
        self.runs.append(record)

    def status_text(self) -> str:
        problems, _ = self._problems()
        if not problems:
            return "evidence bundle complete"
        return "evidence bundle incomplete:" + _problem_lines(problems)

    def check(self, channel: TaskChannel) -> str | None:
        problems, snapshot = self._problems()
        if problems:
            channel.emit(
                "evidence_check", {"complete": False, "problems": [p.code for p in problems]}
            )
            return REFUSAL_PREFIX + _problem_lines(problems)
        digest = snapshot.digest
        diff = unified_diff(self.baseline_dir, self.repo)
        shown, truncated, ref = self._save("diff", diff, head=True)
        files_changed = (
            len(diff_snapshots(self._start, snapshot).paths()) if self._start is not None else 0
        )
        self.sink.add(
            EvidenceRecord(
                kind="diff",
                source="harness",
                output=shown,
                output_truncated=truncated,
                output_ref=ref,
                workspace_digest=digest,
                details={"files_changed": files_changed},
            )
        )
        for run_id, run in zip(reversed(self._run_ids), reversed(self.runs), strict=True):
            if run.workspace_digest == digest and run.covers_target:
                self._post_evidence_id = run_id
                break
        self.checked_digest = digest
        channel.emit("evidence_check", {"complete": True, "problems": []})
        return None

    # --- confirming ------------------------------------------------------------------------

    def confirm(self) -> Confirmation:
        current = take_snapshot(self.repo).digest
        if current != self.checked_digest:
            discrepancy: dict[str, Any] = {
                "reason": "workspace_changed",
                "checked_digest": self.checked_digest,
                "current_digest": current,
            }
            self.channel.emit("harness_discrepancy", discrepancy)
            return Confirmation(False, None, None, discrepancy)
        run = self._execute(harness_command(self.test_command, self.failing_test))
        passed = run.exit_code == 0
        record = self._test_record(
            "harness_rerun",
            "harness",
            run,
            selector=self.failing_test,
            digest=take_snapshot(self.repo).digest,
            covers=True,
            details={
                "post_evidence_id": self._post_evidence_id,
                "expected_exit_code": 0,
                "passed": passed,
            },
        )
        evidence_id = self.sink.add(record)
        self.rerun = record
        self.channel.emit(
            "harness_rerun",
            {"evidence_id": evidence_id, "exit_code": run.exit_code, "passed": passed},
        )
        if passed:
            return Confirmation(True, run.exit_code, evidence_id, None)
        discrepancy = {
            "reason": "rerun_failed",
            "post_evidence_id": self._post_evidence_id,
            "post_exit_code": 0,
            "rerun_exit_code": run.exit_code,
            "rerun_failures": sorted(record.failures),
            "output_tail": run.output[-_RERUN_TAIL:],
        }
        self.channel.emit("harness_discrepancy", discrepancy)
        return Confirmation(False, run.exit_code, evidence_id, discrepancy)
