"""swf_eval unit tests (m9-contract §1.5, §3 T7): metrics and the DoD math, budget (including the
shared counter two driver threads use), the report, the repo allowlist (function and CLI entry
points), the eval JWT, the child-process env and the seed list. No network, no Docker.

`SharedBudget` is not named in the contract (§0.15 only says the counters are "shared under a
lock"); its interface is pinned here, see the T7 report.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

import jwt
import pytest
import yaml
from m9_eval_fakes import ALLOWED_REPO, ROOT, SEED_NAMES, full_run, rec, resolution
from swf_eval.budget import Budget, SharedBudget
from swf_eval.localstack import child_env, mint_jwt
from swf_eval.metrics import TaskRecord, parse_mem_mb, verdict
from swf_eval.report import render_results
from swf_eval.seeds import (
    ALLOWED_EVAL_REPOS,
    FORBIDDEN_REPO,
    SEED_BRANCH_RE,
    check_eval_repo,
    load_seeds,
)

from swf.auth import AuthError, JwtVerifier

# --- metrics and the DoD math (§0.10-§0.12) ------------------------------------------------


def test_ratio_at_limit_passes() -> None:
    v = verdict(full_run(100.0, 125.0, 9, 8))
    assert v.wall_ratio == 1.25 and v.wall_ok
    assert v.drop == pytest.approx(0.1) and v.resolve_ok


def test_ratio_over_limit_fails() -> None:
    v = verdict(full_run(100.0, 126.0, 9, 9))
    assert v.wall_ratio == pytest.approx(1.26) and not v.wall_ok


def test_drop_of_two_fails() -> None:
    assert not verdict(full_run(100.0, 100.0, 9, 7)).resolve_ok


def test_concurrent_better_is_ok() -> None:
    v = verdict(full_run(100.0, 90.0, 7, 9))
    assert v.wall_ok and v.resolve_ok and v.drop == pytest.approx(-0.2)


@pytest.mark.parametrize("single_ok", range(1, 11))
def test_one_seed_drop_always_passes_despite_float_noise(single_ok: int) -> None:
    """0.8 - 0.7 is 0.10000000000000009 in floats; one seed of ten is still within 10 points."""
    v = verdict(full_run(100.0, 100.0, single_ok, single_ok - 1))
    assert v.resolve_ok, (v.rate_single, v.rate_concurrent, v.drop)


@pytest.mark.parametrize("single_ok", range(2, 11))
def test_two_seed_drop_always_fails(single_ok: int) -> None:
    assert not verdict(full_run(100.0, 100.0, single_ok, single_ok - 2)).resolve_ok


def test_drop_is_absolute_points_not_relative_to_the_single_rate() -> None:
    # 2/10 -> 1/10 is a 50% relative drop but one seed (10 points): allowed.
    assert verdict(full_run(100.0, 100.0, 2, 1)).resolve_ok
    # 3/10 -> 1/10 is two seeds (20 points): refused.
    assert not verdict(full_run(100.0, 100.0, 3, 1)).resolve_ok


def test_rates_are_resolved_over_ten() -> None:
    v = verdict(full_run(100.0, 100.0, 6, 4))
    assert v.rate_single == pytest.approx(0.6) and v.rate_concurrent == pytest.approx(0.4)
    assert v.drop == pytest.approx(0.2)


def test_insufficient_walls_fail_closed() -> None:
    records = [
        r
        if r.pass_ == "single" or int(r.seed) < 7
        else dataclasses.replace(r, status="interrupted", claimed_at=None, resolution=None)
        for r in full_run(100.0, 100.0, 9, 9)
    ]
    v = verdict(records)  # 7 concurrent walls < min_tasks 8
    assert v.wall_ratio is None and not v.wall_ok
    assert v.wall_single_median == 100.0


def test_eight_walls_per_pass_is_enough() -> None:
    records = [
        r
        if int(r.seed) < 8
        else dataclasses.replace(r, status="interrupted", claimed_at=None, resolution=None)
        for r in full_run(100.0, 110.0, 9, 9)
    ]
    v = verdict(records)
    assert v.wall_ratio == pytest.approx(1.1) and v.wall_ok


def test_a_task_needs_both_timestamps_to_count() -> None:
    records = [
        dataclasses.replace(r, finished_at=None) if r.pass_ == "single" and int(r.seed) < 3 else r
        for r in full_run(100.0, 100.0, 9, 9)
    ]
    assert verdict(records).wall_ratio is None  # 7 single walls < 8


def test_the_gating_median_includes_failed_tasks_with_both_timestamps() -> None:
    """Five fast failures + five slow completions: median 55. A fast failure cannot hide a
    slowdown only if completed-only is reported next to it (report test below)."""
    single = [
        rec(
            "single", f"{i:02}", 10.0 if i < 5 else 100.0, True, "completed" if i >= 5 else "failed"
        )
        for i in range(10)
    ]
    concurrent = [rec("concurrent", f"{i:02}", 55.0, True) for i in range(10)]
    v = verdict(single + concurrent)
    assert v.wall_single_median == pytest.approx(55.0)
    assert v.wall_concurrent_median == pytest.approx(55.0)
    assert v.wall_ratio == pytest.approx(1.0) and v.wall_ok


def test_medians_are_per_pass_and_robust_to_one_outlier() -> None:
    records = full_run(100.0, 100.0, 9, 9)
    records[3] = dataclasses.replace(records[3], finished_at=5000.0)  # one runaway single task
    v = verdict(records)
    assert v.wall_single_median == 100.0 and v.wall_ok


def test_wall_seconds_is_finish_minus_claim() -> None:
    r = rec("single", "00", 90.0, True, claimed_at=10.0, finished_at=100.0)
    assert r.wall_seconds == pytest.approx(90.0)
    assert rec("single", "00", None, False, "interrupted").wall_seconds is None


def test_resolved_property_follows_the_resolution() -> None:
    assert rec("single", "00", 1.0, True).resolved
    assert not rec("single", "00", 1.0, False).resolved
    assert not rec("single", "00", 1.0, False, "failed").resolved  # no resolution at all


def test_resolution_resolved_needs_setup_target_and_suite() -> None:
    base = resolution(True)
    assert base.resolved
    assert not dataclasses.replace(base, setup_ok=False).resolved
    assert not dataclasses.replace(base, target_exit=1).resolved
    assert not dataclasses.replace(base, target_exit=None).resolved
    assert not dataclasses.replace(base, suite_exit=1).resolved
    assert not dataclasses.replace(base, suite_exit=None).resolved
    assert base.files_added == () and base.tests_touched == ()


def test_missing_pass_counts_unresolved() -> None:
    v = verdict([rec("single", f"{i:02}", 100.0, True) for i in range(10)])
    assert v.rate_concurrent == 0.0 and not v.resolve_ok
    assert v.wall_ratio is None and not v.wall_ok


def test_a_task_that_never_ran_counts_unresolved() -> None:
    records = [
        dataclasses.replace(r, status="cancelled", claimed_at=None, resolution=None)
        if r.pass_ == "concurrent" and int(r.seed) == 0
        else r
        for r in full_run(100.0, 100.0, 10, 10)
    ]
    v = verdict(records)
    assert v.rate_concurrent == pytest.approx(0.9) and v.resolve_ok


def test_batch_wall_is_first_claim_to_last_finish() -> None:
    records = [
        rec("single", "00", None, True, claimed_at=100.0, finished_at=160.0),
        rec("single", "01", None, True, claimed_at=170.0, finished_at=300.0),
        rec("concurrent", "00", None, True, claimed_at=0.0, finished_at=50.0),
        rec("concurrent", "01", None, True, claimed_at=10.0, finished_at=90.0),
    ]
    v = verdict(records, seeds=2, min_tasks=2)
    assert v.batch_single_seconds == pytest.approx(200.0)
    assert v.batch_concurrent_seconds == pytest.approx(90.0)


@pytest.mark.parametrize(
    "raw,mb",
    [
        ("123.4MiB / 512MiB", 123),
        ("123.6MiB / 512MiB", 124),
        ("1.5GiB / 4GiB", 1536),
        ("1GiB / 4GiB", 1024),
        ("900KiB / 512MiB", 1),
        ("1024KiB / 1GiB", 1),
        ("0B / 0B", 0),
        ("512B / 1GiB", 0),
    ],
)
def test_parse_mem(raw: str, mb: int) -> None:
    assert parse_mem_mb(raw) == mb


# --- budget (§0.15) ------------------------------------------------------------------------


def test_budget_first_limit_wins() -> None:
    b = Budget()
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=0, model_failures=0) is None
    assert b.exceeded(usd=1.0, calls=0, prs=0, elapsed=0, model_failures=0) == "budget_usd"
    assert b.exceeded(usd=0, calls=4000, prs=22, elapsed=0, model_failures=0) == "max_calls"
    assert b.exceeded(usd=0, calls=0, prs=22, elapsed=0, model_failures=0) == "max_prs"
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=6 * 3600, model_failures=0) == "max_hours"
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=0, model_failures=5) == "model_failures"


def test_budget_defaults() -> None:
    b = Budget()
    assert (b.max_usd, b.max_calls, b.max_prs, b.max_seconds) == (1.0, 4000, 22, 6 * 3600)
    assert b.max_consecutive_model_failures == 5


def test_budget_just_below_each_limit_is_fine() -> None:
    b = Budget()
    assert b.exceeded(usd=0.99, calls=3999, prs=21, elapsed=6 * 3600 - 1, model_failures=4) is None


def test_budget_order_when_several_limits_are_reached() -> None:
    b = Budget()
    every = dict(usd=5.0, calls=9999, prs=99, elapsed=10**6, model_failures=99)
    assert b.exceeded(**every) == "budget_usd"
    assert b.exceeded(**{**every, "usd": 0}) == "max_calls"
    assert b.exceeded(**{**every, "usd": 0, "calls": 0}) == "max_prs"
    assert b.exceeded(**{**every, "usd": 0, "calls": 0, "prs": 0}) == "max_hours"


def test_budget_custom_limits_are_honoured() -> None:
    b = Budget(max_usd=0.5, max_calls=10, max_prs=2)
    assert b.exceeded(usd=0.5, calls=0, prs=0, elapsed=0, model_failures=0) == "budget_usd"
    assert b.exceeded(usd=0, calls=10, prs=0, elapsed=0, model_failures=0) == "max_calls"
    assert b.exceeded(usd=0, calls=0, prs=2, elapsed=0, model_failures=0) == "max_prs"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_shared_budget_accumulates_and_reports_the_first_limit() -> None:
    clock = FakeClock()
    shared = SharedBudget(Budget(), clock=clock)
    assert shared.exceeded() is None
    shared.add(usd=0.4, calls=100, prs=1)
    shared.add(usd=0.4, calls=100, prs=1)
    assert (shared.usd, shared.calls, shared.prs) == (pytest.approx(0.8), 200, 2)
    assert shared.exceeded() is None
    shared.add(usd=0.2)
    assert shared.exceeded() == "budget_usd"


def test_shared_budget_elapsed_comes_from_the_clock() -> None:
    clock = FakeClock()
    shared = SharedBudget(Budget(), clock=clock)
    clock.now += 6 * 3600 - 1
    assert shared.exceeded() is None
    clock.now += 1
    assert shared.exceeded() == "max_hours"


def test_shared_budget_counts_consecutive_model_failures_only() -> None:
    shared = SharedBudget(Budget(), clock=FakeClock())
    for _ in range(4):
        shared.task_done(model_failed=True)
    assert shared.exceeded() is None
    shared.task_done(model_failed=False)  # a success resets the streak
    for _ in range(4):
        shared.task_done(model_failed=True)
    assert shared.exceeded() is None
    shared.task_done(model_failed=True)
    assert shared.exceeded() == "model_failures"


def test_shared_budget_counters_are_thread_safe() -> None:
    shared = SharedBudget(Budget(max_calls=10**9), clock=FakeClock())

    def work() -> None:
        for _ in range(500):
            shared.add(calls=1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert shared.calls == 4000


def test_two_driver_threads_stop_once_the_shared_cost_crosses_the_limit() -> None:
    """Both drivers are mid-task when the sum crosses $1: neither creates another task."""
    shared = SharedBudget(Budget(max_usd=1.0), clock=FakeClock())
    created: list[tuple[str, int]] = []
    lock = threading.Lock()
    in_task = threading.Barrier(2, timeout=10)
    cost_booked = threading.Barrier(2, timeout=10)

    def driver(name: str) -> None:
        for n in range(5):
            if shared.exceeded() is not None:
                return
            with lock:
                created.append((name, n))
            if n == 0:
                in_task.wait()
            shared.add(usd=0.6)  # each task costs $0.60: $0.60 alone is fine, $1.20 together is not
            if n == 0:
                cost_booked.wait()

    threads = [threading.Thread(target=driver, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(created) == [("a", 0), ("b", 0)]
    assert shared.exceeded() == "budget_usd"


def test_a_driver_mid_task_sees_the_other_threads_overrun_at_its_periodic_check() -> None:
    shared = SharedBudget(Budget(max_usd=1.0), clock=FakeClock())
    seen: list[str | None] = []
    shared.add(usd=1.0)  # the other thread's task finished and crossed the limit
    t = threading.Thread(target=lambda: seen.append(shared.exceeded()))
    t.start()
    t.join(timeout=10)
    assert seen == ["budget_usd"]


# --- report (§1.6) -------------------------------------------------------------------------

TOKENS = ["ghp_" + "A" * 36, "gho_" + "B" * 36, "github_pat_" + "C" * 40, "sk-" + "D" * 40]
SECRET_SHAPES = re.compile(r"ghp_|gho_|github_pat_|(?<![A-Za-z0-9])sk-[A-Za-z0-9]")


def flag(pass_: str, seed: str, category: str, source: str) -> dict[str, str]:
    return {"pass": pass_, "seed": seed, "category": category, "source": source}


FLAGS = [
    flag("single", "01-paginate-off-by-one", "injection", "tool_output"),
    flag("single", "02-mutable-default", "injection", "repo_file"),
    flag("concurrent", "03-none-last-name", "pii", "model_output"),
    flag("concurrent", "04-average-int-division", "secret", "tool_output"),
]


def run_info(**overrides: object) -> dict[str, object]:
    info: dict[str, object] = {
        "run_id": "20261122-r1x2y3",
        "date": "2026-11-22",
        "git_sha": "0123abc",
        "repo": ALLOWED_REPO,
        "host": {"ncpu": 8, "mem_total": 16 * 2**30, "runtime": "runc"},
        "model": "@glm-4-7-flash/glm-4.7-flash",
        "agent_limits": {"step_limit": 80, "wall_time_limit_seconds": 1800},
        "budget": {"usd": 0.0, "calls": 612, "prs": 20, "elapsed_seconds": 5400.0},
        "aborted": None,
        "guard_flags": FLAGS,
        "controls": [
            {"seed": s, "pristine_target_exit": 1, "reference_resolved": True} for s in SEED_NAMES
        ],
        "replaced": [],
        "notes": [],
        "cleanup": [],
    }
    info.update(overrides)
    return info


def task_rows(text: str) -> list[str]:
    """Data rows of the per-task table: from its `| Pass |` header to the next non-table line."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^\|\s*Pass\s*\|", ln))
    rows: list[str] = []
    for ln in lines[start + 2 :]:  # header, separator
        if not ln.startswith("|"):
            break
        rows.append(ln)
    return rows


def render(records: Sequence[TaskRecord], **overrides: object) -> str:
    return render_results(run_info(**overrides), records, verdict(records))


def test_report_has_summary_header_twenty_task_rows_and_the_dod_limits() -> None:
    text = render(full_run(100.0, 110.0, 9, 8))
    assert "| Metric | Single | Concurrent | Value | DoD | Met |" in text
    assert len(task_rows(text)) == 20
    assert "≤ 1.25" in text and "≤ 10" in text
    for metric in (
        "Median per-task wall (s)",
        "Resolve rate",
        "Batch wall (s)",
        "Agent calls / guard chunks",
        "Cost (booked $)",
        "Median wall, completed only (s)",
    ):
        assert metric in text, metric
    assert re.search(r"9\s*/\s*10", text) and re.search(r"8\s*/\s*10", text)
    for column in ("Tests touched", "Files added", "Target", "Suite", "Resolved", "Peak MB"):
        assert column in text, column


def test_report_task_rows_are_sorted_by_pass_then_seed() -> None:
    records = list(reversed(full_run(100.0, 100.0, 9, 9)))
    keys = [tuple(c.strip() for c in row.split("|")[1:3]) for row in task_rows(render(records))]
    assert keys == sorted(keys, key=lambda k: (k[0] != "single", k[1]))  # single first, by seed
    assert keys[0] == ("single", "00") and keys[-1] == ("concurrent", "09")


def test_report_header_names_the_run_model_and_limits() -> None:
    text = render(full_run(100.0, 100.0, 9, 9))
    for needle in ("20261122-r1x2y3", "0123abc", "glm-4.7-flash", "runc"):
        assert needle in text, needle
    assert re.search(r"\b80\b", text) and re.search(r"\b1800\b", text)


def test_report_states_the_abort_reason() -> None:
    assert "budget_usd" in render(full_run(100.0, 100.0, 9, 9), aborted="budget_usd")


def test_report_median_over_completed_tasks_sits_next_to_the_gating_median() -> None:
    single = [
        rec(
            "single", f"{i:02}", 10.0 if i < 5 else 100.0, True, "completed" if i >= 5 else "failed"
        )
        for i in range(10)
    ]
    concurrent = [rec("concurrent", f"{i:02}", 120.0, True) for i in range(10)]
    text = render(single + concurrent)
    gating = next(ln for ln in text.splitlines() if "Median per-task wall (s)" in ln)
    completed = next(ln for ln in text.splitlines() if "Median wall, completed only (s)" in ln)
    assert re.search(r"\b55(\.0+)?\b", gating)  # all five fast failures count in the gating median
    assert re.search(r"\b100(\.0+)?\b", completed)  # the five completions took 100 s
    assert re.search(r"\b120(\.0+)?\b", completed)


def test_report_shows_approvals_per_kind_files_added_and_tests_touched() -> None:
    records = full_run(100.0, 100.0, 10, 10)
    records[0] = dataclasses.replace(
        records[0],
        approvals={"plan": 1, "command": 3, "guard_flag": 2, "readonly_violation": 0},
        questions=2,
        resolution=resolution(
            False, tests_touched=("tests/test_pager.py",), files_added=("pytest.py",)
        ),
    )
    text = render(records)
    first = task_rows(text)[0]
    assert re.search(r"1\s*/\s*3\s*/\s*2\s*/\s*0", first)
    assert "tests/test_pager.py" in first and "pytest.py" in first


def test_report_counts_auto_approved_guard_flags_by_category_and_lists_each() -> None:
    text = render(full_run(100.0, 100.0, 9, 9))
    for category, count in (("injection", 2), ("pii", 1), ("secret", 1)):
        assert any(category in ln and re.search(rf"\b{count}\b", ln) for ln in text.splitlines()), (
            category,
            count,
        )
    for flag in FLAGS:
        assert any(
            all(str(flag[k]) in ln for k in ("seed", "category", "source"))
            for ln in text.splitlines()
        ), flag


def test_report_lists_replaced_records_with_their_cause_and_keeps_them_out_of_the_table() -> None:
    replaced = [
        {
            "pass": "single",
            "seed": "03-none-last-name",
            "task_id": "11111111-2222-3333-4444-555555555555",
            "cause": "clone_failed",
            "status": "failed",
        }
    ]
    text = render(full_run(100.0, 100.0, 9, 9), replaced=replaced)
    assert "eplaced" in text
    assert "clone_failed" in text and "11111111-2222-3333-4444-555555555555" in text
    assert len(task_rows(text)) == 20
    assert not any("clone_failed" in row for row in task_rows(text))


def test_report_lists_the_controls_for_every_seed() -> None:
    text = render(full_run(100.0, 100.0, 9, 9))
    for name in SEED_NAMES:
        assert name in text, name


def test_report_redacts_token_shaped_strings() -> None:
    records = full_run(100.0, 100.0, 9, 9)
    records[2] = dataclasses.replace(
        records[2], status="failed", error="push failed: " + " ".join(TOKENS), resolution=None
    )
    notes = [f"clone said {t}" for t in TOKENS]
    replaced = [
        {"pass": "single", "seed": "05-top-scores-order", "task_id": "t", "cause": TOKENS[0]}
    ]
    text = render(records, notes=notes, replaced=replaced)
    assert SECRET_SHAPES.search(text) is None
    assert "push failed" in text  # the message stays, only the token goes


# --- repo allowlist (§0.8, §1.5) -----------------------------------------------------------


def test_the_allowlist_constants() -> None:
    assert FORBIDDEN_REPO == "shreyaasai/fixable"
    assert frozenset({"ShreyaasAI/swf-pr-sandbox"}) == ALLOWED_EVAL_REPOS


@pytest.mark.parametrize(
    "repo",
    [
        "ShreyaasAI/swf-pr-sandbox",
        "shreyaasai/SWF-PR-SANDBOX",
        "https://github.com/ShreyaasAI/swf-pr-sandbox",
        "https://github.com/ShreyaasAI/swf-pr-sandbox.git",
    ],
)
def test_check_eval_repo_accepts_the_sandbox(repo: str) -> None:
    assert check_eval_repo(repo).lower() == "shreyaasai/swf-pr-sandbox"


@pytest.mark.parametrize(
    "repo",
    [
        "ShreyaasAI/Fixable",
        "shreyaasai/FIXABLE",
        "https://github.com/ShreyaasAI/Fixable.git",
        "ShreyaasAI/swf-ci",
        "other/swf-pr-sandbox",
        "ShreyaasAI/swf-pr-sandbox-2",
        "ShreyaasAI/swf-pr-sandbox/extra",
        "swf-pr-sandbox",
        "",
        "../ShreyaasAI/swf-pr-sandbox",
    ],
)
def test_check_eval_repo_refuses_everything_else(repo: str) -> None:
    with pytest.raises(ValueError):
        check_eval_repo(repo)


# Each CLI entry point refuses a disallowed repo before any git or network call. The child runs
# with a `sitecustomize` that logs every subprocess start and every non-loopback DNS lookup or
# connect, so "before any call" is checked on the real process, not on a mock.
GUARD = """\
import os
import socket
import subprocess

_LOG = os.environ["SWF_TEST_CALL_LOG"]
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def _note(what):
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(what + "\\n")


_popen = subprocess.Popen.__init__


def _popen_init(self, *args, **kwargs):
    _note("subprocess")
    return _popen(self, *args, **kwargs)


subprocess.Popen.__init__ = _popen_init
_getaddrinfo = socket.getaddrinfo


def _gai(host, *args, **kwargs):
    if host not in _LOOPBACK:
        _note("dns:" + str(host))
    return _getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _gai
_connect = socket.socket.connect


def _conn(self, address):
    if isinstance(address, tuple) and address[0] not in _LOOPBACK:
        _note("connect:" + str(address[0]))
    return _connect(self, address)


socket.socket.connect = _conn
"""  # noqa: E501
USAGE_ERRORS = ("unrecognized arguments", "invalid choice", "the following arguments are required")


class GuardedPython:
    """Runs `python <argv>` with the call-logging `sitecustomize` first on PYTHONPATH."""

    def __init__(self, tmp_path: Path) -> None:
        guard = tmp_path / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_bytes(GUARD.encode("utf-8"))
        self.log = tmp_path / "calls.log"
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(guard), self.env.get("PYTHONPATH", "")])
        )
        self.env["SWF_TEST_CALL_LOG"] = str(self.log)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def python(self, *argv: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        proc = subprocess.run(
            [sys.executable, *argv],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return proc, calls

    def cli(self, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        return self.python("-m", "swf_eval", *args)


@pytest.fixture
def guarded(tmp_path: Path) -> GuardedPython:
    return GuardedPython(tmp_path)


def test_the_call_guard_records_a_subprocess_start(guarded: GuardedPython) -> None:
    """Control: a child that starts a process is logged, so an empty log means something."""
    code = "import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'], check=True)"
    proc, calls = guarded.python("-c", code)
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "subprocess" in calls


def test_cli_help_makes_no_call(guarded: GuardedPython) -> None:
    proc, calls = guarded.cli("--help")
    assert proc.returncode == 0, proc.stderr[-300:]
    assert calls == []


REFUSED = [
    "ShreyaasAI/Fixable",
    "shreyaasai/fixable",
    "https://github.com/ShreyaasAI/Fixable",
    "ShreyaasAI/swf-ci",
    "other/swf-pr-sandbox",
]


@pytest.mark.parametrize("repo", REFUSED)
def test_run_refuses_a_disallowed_repo_before_any_call(
    guarded: GuardedPython, tmp_path: Path, repo: str
) -> None:
    proc, calls = guarded.cli(
        "run", "--pass", "both", "--repo", repo, "--out", str(tmp_path / "out")
    )
    assert proc.returncode != 0
    assert not any(m in proc.stderr for m in USAGE_ERRORS), proc.stderr[-300:]
    assert calls == []


@pytest.mark.parametrize("repo", REFUSED)
def test_seed_branches_refuses_a_disallowed_repo_before_any_call(
    guarded: GuardedPython, repo: str
) -> None:
    proc, calls = guarded.cli("seed-branches", "--repo", repo)
    assert proc.returncode != 0
    assert not any(m in proc.stderr for m in USAGE_ERRORS), proc.stderr[-300:]
    assert calls == []


@pytest.mark.parametrize("command", ["check", "cleanup"])
@pytest.mark.parametrize("repo", ["ShreyaasAI/Fixable", "other/swf-pr-sandbox"])
def test_check_and_cleanup_refuse_the_repo_recorded_in_run_json(
    guarded: GuardedPython, tmp_path: Path, command: str, repo: str
) -> None:
    """`check` and `cleanup` take `--out` only; the repo is the one `run` recorded in run.json."""
    out = tmp_path / "out"
    (out / "tasks").mkdir(parents=True)
    (out / "run.json").write_bytes(
        json.dumps({"repo": repo, "run_id": "x", "tasks": [], "replaced": []}).encode()
    )
    args = [command, "--out", str(out)]
    if command == "cleanup":
        args.append("--apply")
    proc, calls = guarded.cli(*args)
    assert proc.returncode != 0
    assert not any(m in proc.stderr for m in USAGE_ERRORS), proc.stderr[-300:]
    assert calls == []


# --- eval JWT, child env, seeds ------------------------------------------------------------

SECRET = "eval-secret-" + "x" * 32


def test_mint_jwt_claims_decode_with_the_secret() -> None:
    uid = uuid.uuid4()
    now = int(time.time())
    token = mint_jwt(SECRET, user_id=uid, github_id=424242, login="eval-a", now=now)
    claims = jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience="authenticated",
        issuer="http://eval.localhost/auth/v1",
    )
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    assert claims["sub"] == str(uid)
    assert claims["aud"] == "authenticated" and claims["role"] == "authenticated"
    assert claims["iss"] == "http://eval.localhost/auth/v1"
    assert claims["user_metadata"]["provider_id"] == "424242"
    assert claims["user_metadata"]["user_name"] == "eval-a"
    assert claims["exp"] == now + 12 * 3600


def test_mint_jwt_is_accepted_by_the_real_verifier() -> None:
    uid = uuid.uuid4()
    token = mint_jwt(SECRET, user_id=uid, github_id=7, login="eval-b", now=int(time.time()))
    verifier = JwtVerifier(
        issuer="http://eval.localhost/auth/v1", key_resolver=None, hs256_secret=SECRET
    )
    principal = verifier.verify(token)
    assert principal.user_id == uid and principal.github_user_id == 7
    assert principal.github_login == "eval-b"


def test_mint_jwt_signed_with_another_secret_is_refused() -> None:
    token = mint_jwt(
        "another-secret-" + "y" * 32,
        user_id=uuid.uuid4(),
        github_id=7,
        login="u",
        now=int(time.time()),
    )
    verifier = JwtVerifier(
        issuer="http://eval.localhost/auth/v1", key_resolver=None, hs256_secret=SECRET
    )
    with pytest.raises(AuthError):
        verifier.verify(token)


def test_child_env_drops_everything_not_allowlisted() -> None:
    base = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": r"C:\Windows",
        "SWF_SUPABASE_DB_URL": "postgresql://secret@prod/db",
        "DATABASE_URL": "postgresql://x",
        "GITHUB_TOKEN": "leak1",
        "GH_TOKEN": "leak2",
        "SWF_GIT_TOKEN": "leak3",
        "SWF_SUPABASE_JWT_SECRET": "leak4",
        "ANTHROPIC_API_KEY": "leak5",
        "AWS_SECRET_ACCESS_KEY": "leak6",
        "HOME": "/home/u",
    }
    extra = {"SWF_DATABASE_URL": "postgresql://local", "SWF_QUOTA_TASKS_PER_DAY": "12"}
    env = child_env(base, extra)
    assert "SWF_SUPABASE_DB_URL" not in env
    assert not any(v.startswith("leak") for v in env.values())
    assert env["PATH"] == "/usr/bin"
    assert env["SWF_DATABASE_URL"] == "postgresql://local"
    assert env["SWF_QUOTA_TASKS_PER_DAY"] == "12"
    assert set(env) <= {"PATH", "SYSTEMROOT", "PORTKEY_API_KEY", *extra}


def test_child_env_extra_wins_over_base() -> None:
    env = child_env({"PATH": "/a"}, {"PATH": "/b"})
    assert env["PATH"] == "/b"


def test_child_env_never_mutates_its_inputs() -> None:
    base, extra = {"PATH": "/a", "X": "1"}, {"Y": "2"}
    child_env(base, extra)
    assert base == {"PATH": "/a", "X": "1"} and extra == {"Y": "2"}


def test_load_seeds_returns_the_ten_seeded_repos() -> None:
    seeds = load_seeds()
    assert [s.name for s in seeds] == SEED_NAMES
    for seed in seeds:
        data = yaml.safe_load((ROOT / "seeded_repos" / seed.name / "seed.yaml").read_text("utf-8"))
        assert seed.target_test == data["target_test"] and "::" in seed.target_test
        assert seed.description == data["description"] and seed.description
        assert seed.repo_dir == ROOT / "seeded_repos" / seed.name / "repo"
        assert seed.reference_dir == ROOT / "seeded_repos" / seed.name / "reference"
        assert (seed.repo_dir / "AGENTS.md").is_file() and seed.reference_dir.is_dir()


def test_seed_branch_regex_accepts_only_seed_names() -> None:
    for name in SEED_NAMES:
        assert SEED_BRANCH_RE.fullmatch(f"seed/{name}")
    for bad in (
        "agent/12345678-1234-5678-1234-567812345678",
        "seed/../x",
        "+seed/01-a",
        "main",
        "seed/1-a",
        "seed/01-A",
        "seed/01-a/b",
        "seed/01-a\n",
    ):
        assert not SEED_BRANCH_RE.fullmatch(bad), bad
