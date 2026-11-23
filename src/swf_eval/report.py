"""`docs/software-factory/m9-results.md` (m9-contract §1.6)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from swf_eval.metrics import TaskRecord, Verdict, median_wall

_TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]*|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]*")
_APPROVAL_KINDS = ("plan", "command", "guard_flag", "readonly_violation")


def redact_tokens(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", text)


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _yes(ok: bool) -> str:
    return "yes" if ok else "no"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _ratio(a: float | None, b: float | None) -> str:
    return "n/a" if a is None or not b else f"{a / b:.2f}"


def render_results(run: Mapping[str, Any], records: Sequence[TaskRecord], v: Verdict) -> str:
    host = run.get("host") or {}
    limits = run.get("agent_limits") or {}
    budget = run.get("budget") or {}
    by_pass = {p: [r for r in records if r.pass_ == p] for p in ("single", "concurrent")}
    seeds = 10
    out: list[str] = [
        "# M9 results",
        "",
        f"- Run id: {run.get('run_id')}",
        f"- Date: {run.get('date')}",
        f"- Git sha: {run.get('git_sha')}",
        f"- Repo: {run.get('repo')}",
        f"- Host: NCPU {host.get('ncpu')}, MemTotal {host.get('mem_total')}, "
        f"runtime {host.get('runtime')}",
        f"- Model: {run.get('model')}",
        f"- Agent limits: step_limit {limits.get('step_limit')}, "
        f"wall_time_limit_seconds {limits.get('wall_time_limit_seconds')}",
        f"- Budget used: ${budget.get('usd')}, {budget.get('calls')} calls, "
        f"{budget.get('prs')} PRs, {budget.get('elapsed_seconds')} s",
        f"- Abort reason: {run.get('aborted') or 'none'}",
        f"- Portkey usage before/after: {run.get('portkey_usage') or '(filled in by the user)'}",
        "",
        "## Summary",
        "",
        "| Metric | Single | Concurrent | Value | DoD | Met |",
        "|---|---|---|---|---|---|",
    ]
    rs = round(v.rate_single * seeds)
    rc = round(v.rate_concurrent * seeds)
    cost = {p: sum(r.cost_usd for r in rs_) for p, rs_ in by_pass.items()}
    total_cost = cost["single"] + cost["concurrent"]
    completed = {p: median_wall(records, p, completed_only=True) for p in by_pass}
    calls = {
        p: f"{sum(r.agent_calls for r in rs_)} / {sum(r.guard_chunks for r in rs_)}"
        for p, rs_ in by_pass.items()
    }
    out += [
        _row(
            [
                "Median per-task wall (s)",
                _num(v.wall_single_median),
                _num(v.wall_concurrent_median),
                _num(v.wall_ratio, 2),
                "≤ 1.25",
                _yes(v.wall_ok),
            ]
        ),
        _row(
            [
                "Median wall, completed only (s)",
                _num(completed["single"]),
                _num(completed["concurrent"]),
                _ratio(completed["concurrent"], completed["single"]),
                "info",
                "—",
            ]
        ),
        _row(
            [
                "Resolve rate",
                f"{rs}/{seeds}",
                f"{rc}/{seeds}",
                f"{v.drop * 100:.0f} points",
                "≤ 10",
                _yes(v.resolve_ok),
            ]
        ),
        _row(
            [
                "Batch wall (s)",
                _num(v.batch_single_seconds),
                _num(v.batch_concurrent_seconds),
                _ratio(v.batch_concurrent_seconds, v.batch_single_seconds),
                "info",
                "—",
            ]
        ),
        _row(
            ["Agent calls / guard chunks", calls["single"], calls["concurrent"], "—", "info", "—"]
        ),
        _row(
            [
                "Cost (booked $)",
                f"{cost['single']:.4f}",
                f"{cost['concurrent']:.4f}",
                f"{total_cost:.4f}",
                "≤ $1 total",
                _yes(total_cost <= 1.0),
            ]
        ),
        "",
        "## Per task",
        "",
        "| Pass | Seed | User | Status | Error | Wall s | Guard s | Agent calls | Retries "
        "| Approvals (plan/cmd/flag/ro) | Questions | Cost $ | PR | Tests touched | Files added "
        "| Target | Suite | Resolved | Peak MB |",
        "|" + "---|" * 19,
    ]
    ordered = sorted(records, key=lambda r: (r.pass_ != "single", r.seed))
    for r in ordered:
        res = r.resolution
        approvals = " / ".join(str(r.approvals.get(k, 0)) for k in _APPROVAL_KINDS)
        out.append(
            _row(
                [
                    r.pass_,
                    r.seed,
                    r.user,
                    r.status,
                    r.error or "",
                    _num(r.wall_seconds),
                    _num(r.guard_seconds),
                    r.agent_calls,
                    r.retries,
                    approvals,
                    r.questions,
                    f"{r.cost_usd:.4f}",
                    r.pr_url or "",
                    ", ".join(res.tests_touched) if res else "",
                    ", ".join(res.files_added) if res else "",
                    "" if res is None or res.target_exit is None else res.target_exit,
                    "" if res is None or res.suite_exit is None else res.suite_exit,
                    _yes(r.resolved),
                    "" if r.peak_mem_mb is None else r.peak_mem_mb,
                ]
            )
        )

    flags: Sequence[Mapping[str, Any]] = run.get("guard_flags") or []
    out += ["", "## Auto-approved guard flags", ""]
    counts = Counter(str(f.get("category")) for f in flags)
    out += [f"- {category}: {n}" for category, n in sorted(counts.items())] or ["- none"]
    out += [""]
    out += [
        f"- {f.get('pass')}, {f.get('seed')}, {f.get('category')}, {f.get('source')}" for f in flags
    ]

    out += ["", "## Controls", "", "| Seed | Pristine target exit | Reference resolved |"]
    out += ["|---|---|---|"]
    for c in run.get("controls") or []:
        out.append(
            _row(
                [
                    c.get("seed"),
                    c.get("pristine_target_exit"),
                    _yes(bool(c.get("reference_resolved"))),
                ]
            )
        )

    out += ["", "## Replaced records", ""]
    replaced: Sequence[Mapping[str, Any]] = run.get("replaced") or []
    out += [
        f"- {x.get('pass')}, {x.get('seed')}, task {x.get('task_id')}, cause: {x.get('cause')}"
        + (f", status {x['status']}" if x.get("status") else "")
        for x in replaced
    ] or ["- none"]

    out += ["", "## Notes", ""]
    out += [f"- {n}" for n in run.get("notes") or []] or ["- none"]
    out += ["", "## PR cleanup log", ""]
    out += [f"- {n}" for n in run.get("cleanup") or []] or ["- none"]
    return redact_tokens("\n".join(out) + "\n")
