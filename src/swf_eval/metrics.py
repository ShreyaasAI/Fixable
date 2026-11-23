"""Per-task records and the M9 DoD verdict (m9-contract §0.10-§0.12, §1.5)."""

from __future__ import annotations

import math
import re
import statistics
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from swf.states import TERMINAL_STATUSES
from swf_eval.resolve import Resolution

Pass = Literal["single", "concurrent"]
_EPS = 1e-9


@dataclass(frozen=True)
class TaskRecord:
    pass_: Pass
    seed: str
    user: str
    task_id: str
    status: str
    error: str | None
    claimed_at: float | None
    finished_at: float | None
    agent_calls: int
    guard_chunks: int
    guard_seconds: float
    retries: int
    approvals: dict[str, int]
    questions: int
    cost_usd: float
    pr_url: str | None
    resolution: Resolution | None
    peak_mem_mb: int | None

    @property
    def wall_seconds(self) -> float | None:
        if self.claimed_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.claimed_at

    @property
    def resolved(self) -> bool:
        return self.resolution is not None and self.resolution.resolved


@dataclass(frozen=True)
class Verdict:
    wall_single_median: float | None
    wall_concurrent_median: float | None
    wall_ratio: float | None
    wall_ok: bool
    rate_single: float
    rate_concurrent: float
    drop: float
    resolve_ok: bool
    batch_single_seconds: float | None
    batch_concurrent_seconds: float | None


def median_wall(
    records: Sequence[TaskRecord], pass_: str, *, min_tasks: int = 1, completed_only: bool = False
) -> float | None:
    walls = [
        w
        for r in records
        if r.pass_ == pass_
        and (not completed_only or r.status == "completed")
        and (w := r.wall_seconds) is not None
    ]
    return statistics.median(walls) if walls and len(walls) >= min_tasks else None


def _batch(records: Sequence[TaskRecord], pass_: str) -> float | None:
    claims = [r.claimed_at for r in records if r.pass_ == pass_ and r.claimed_at is not None]
    ends = [r.finished_at for r in records if r.pass_ == pass_ and r.finished_at is not None]
    return max(ends) - min(claims) if claims and ends else None


def verdict(
    records: Sequence[TaskRecord],
    *,
    seeds: int = 10,
    max_ratio: float = 1.25,
    max_drop: float = 0.10,
    min_tasks: int = 8,
) -> Verdict:
    single = median_wall(records, "single", min_tasks=min_tasks)
    concurrent = median_wall(records, "concurrent", min_tasks=min_tasks)
    ratio = concurrent / single if single and concurrent is not None else None
    rates = {
        p: len({r.seed for r in records if r.pass_ == p and r.resolved}) / seeds
        for p in ("single", "concurrent")
    }
    drop = rates["single"] - rates["concurrent"]
    return Verdict(
        wall_single_median=single,
        wall_concurrent_median=concurrent,
        wall_ratio=ratio,
        wall_ok=ratio is not None and ratio <= max_ratio + _EPS,
        rate_single=rates["single"],
        rate_concurrent=rates["concurrent"],
        drop=drop,
        resolve_ok=drop <= max_drop + _EPS,
        batch_single_seconds=_batch(records, "single"),
        batch_concurrent_seconds=_batch(records, "concurrent"),
    )


def records_from_db(conn: Any, task_ids: Mapping[str, tuple[Pass, str, str]]) -> list[TaskRecord]:
    """One record per task id (`{id: (pass, seed, user)}`) from `tasks` and its event log."""
    out: list[TaskRecord] = []
    for task_id, (pass_, seed, user) in task_ids.items():
        tid = uuid.UUID(task_id)
        task = conn.execute(
            "select status, error, cost_usd, pr_url from public.tasks where id = %s", [tid]
        ).fetchone()
        if task is None:
            raise LookupError(f"task {task_id} not found")
        events = conn.execute(
            "select type, payload, created_at from public.events where task_id = %s order by seq",
            [tid],
        ).fetchall()
        claimed: float | None = None
        finished: float | None = None
        approvals: Counter[str] = Counter()
        agent_calls = guard_chunks = retries = questions = 0
        guard_seconds = 0.0
        for ev in events:
            kind, payload = ev["type"], ev["payload"] or {}
            if kind == "status":
                if payload.get("to") == "preparing" and claimed is None:
                    claimed = ev["created_at"].timestamp()
                elif payload.get("to") in TERMINAL_STATUSES:
                    finished = ev["created_at"].timestamp()
            elif kind == "message" and payload.get("role") == "assistant":
                agent_calls += 1
            elif kind == "guard_check":
                guard_chunks += int(payload.get("chunks") or 0)
                guard_seconds += float(payload.get("latency_ms") or 0) / 1000
            elif kind == "retry":
                retries += 1
            elif kind == "approval_requested":
                approvals[str(payload.get("kind"))] += 1
            elif kind == "question_asked":
                questions += 1
        out.append(
            TaskRecord(
                pass_=pass_,
                seed=seed,
                user=user,
                task_id=task_id,
                status=str(task["status"]),
                error=task["error"],
                claimed_at=claimed,
                finished_at=finished,
                agent_calls=agent_calls,
                guard_chunks=guard_chunks,
                guard_seconds=guard_seconds,
                retries=retries,
                approvals=dict(approvals),
                questions=questions,
                cost_usd=float(task["cost_usd"] or 0),
                pr_url=task["pr_url"],
                resolution=None,
                peak_mem_mb=None,
            )
        )
    return out


_MEM_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B|[kKMGT]B)\b", re.ASCII)
_UNITS = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_DECIMAL = {"B": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def parse_mem_mb(mem_usage: str) -> int:
    """Used memory of a `docker stats` MemUsage cell in MiB, rounded half up."""
    match = _MEM_RE.match(mem_usage)
    if match is None:
        raise ValueError(f"unrecognised memory usage {mem_usage!r}")
    value, unit = float(match.group(1)), match.group(2)
    prefix = unit[0].upper() if len(unit) > 1 else "B"
    scale = (_UNITS if "i" in unit or unit == "B" else _DECIMAL)[prefix]
    return math.floor(value * scale / 1024**2 + 0.5)
