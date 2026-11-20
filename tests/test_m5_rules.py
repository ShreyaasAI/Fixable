"""Pure rules of the evidence bundle (m5-contract §2, §0.10, §0.11): failure parsing, target
coverage, the harness command, and `bundle_problems`."""

from __future__ import annotations

import shlex

import pytest
from m4_fakes import FAILING_TEST, TEST_COMMAND
from m5_fakes import OTHER_FAILURE, fake_pytest_output
from swf.evidence import (
    HARNESS_PREAMBLE,
    BundleProblem,
    EvidenceRecord,
    bundle_problems,
    covers_target,
    harness_command,
    parse_failures,
)

from swf.agent.channel import NoTestCommand
from swf.agent.tools import ToolError

CURRENT = "c" * 64
START = "5" * 64
OLD = "0" * 64


# --- parse_failures -------------------------------------------------------------------------


def test_parse_failures_reads_failed_and_error_summary_lines() -> None:
    output = (
        "=== short test summary info ===\n"
        "FAILED tests/a.py::test_one - AssertionError: 1 != 2\n"
        "ERROR tests/b.py::test_two\n"
        "ERROR tests/c.py - ImportError: no module\n"
        "2 failed, 1 error in 0.1s\n"
    )
    assert parse_failures(output) == frozenset(
        {"tests/a.py::test_one", "tests/b.py::test_two", "tests/c.py"}
    )


def test_parse_failures_strips_the_message_after_space_dash_space() -> None:
    assert parse_failures("FAILED tests/a.py::t - assert 'x' - 'y'\n") == {"tests/a.py::t"}


def test_parse_failures_keeps_parametrized_ids() -> None:
    out = "FAILED tests/a.py::test_t[a-b] - assert 1 == 2\nFAILED tests/a.py::test_t[c-d]\n"
    assert parse_failures(out) == {"tests/a.py::test_t[a-b]", "tests/a.py::test_t[c-d]"}


def test_parse_failures_is_empty_when_nothing_failed() -> None:
    assert parse_failures(fake_pytest_output()) == frozenset()
    assert parse_failures("") == frozenset()


def test_parse_failures_reads_realistic_pytest_output() -> None:
    assert parse_failures(fake_pytest_output(FAILING_TEST, OTHER_FAILURE)) == {
        FAILING_TEST,
        OTHER_FAILURE,
    }


def test_parse_failures_returns_a_frozenset() -> None:
    assert isinstance(parse_failures("FAILED a::b"), frozenset)


# --- covers_target --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "failing_test", "expected"),
    [
        (None, FAILING_TEST, True),
        (FAILING_TEST, FAILING_TEST, True),
        (FAILING_TEST + "[case]", FAILING_TEST, True),
        ("tests/test_pager.py", FAILING_TEST, False),
        ("tests/test_pager.py::test_first_page", FAILING_TEST, False),
        ("tests/other.py::test_x", FAILING_TEST, False),
        (None, None, True),
        (FAILING_TEST, None, False),
        ("tests/other.py", None, False),
    ],
)
def test_covers_target_table(
    selector: str | None, failing_test: str | None, expected: bool
) -> None:
    assert covers_target(selector, failing_test) is expected


# --- harness_command ------------------------------------------------------------------------


def test_harness_command_is_the_preamble_plus_the_test_command() -> None:
    assert harness_command(TEST_COMMAND, None) == HARNESS_PREAMBLE + TEST_COMMAND


def test_harness_command_appends_the_shell_quoted_selector() -> None:
    command = harness_command(TEST_COMMAND, FAILING_TEST)
    assert command == f"{HARNESS_PREAMBLE}{TEST_COMMAND} {shlex.quote(FAILING_TEST)}"


def test_the_preamble_disables_bytecode_and_clears_caches() -> None:
    assert "PYTHONDONTWRITEBYTECODE=1" in HARNESS_PREAMBLE
    assert "__pycache__" in HARNESS_PREAMBLE
    assert ".pytest_cache" in HARNESS_PREAMBLE


def test_harness_command_rejects_an_invalid_selector() -> None:
    with pytest.raises(ToolError):
        harness_command(TEST_COMMAND, "tests/x.py; rm -rf /")


@pytest.mark.parametrize("test_command", [None, ""])
def test_harness_command_without_a_test_command_is_no_test_command(
    test_command: str | None,
) -> None:
    with pytest.raises(NoTestCommand):
        harness_command(test_command, None)


# --- bundle_problems ------------------------------------------------------------------------


def rec(
    kind: str,
    *,
    exit_code: int,
    selector: str | None = None,
    digest: str | None = CURRENT,
    failures: frozenset[str] = frozenset(),
    covers: bool | None = None,
    source: str = "agent_run",
) -> EvidenceRecord:
    return EvidenceRecord(
        kind=kind,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        selector=selector,
        exit_code=exit_code,
        failures=failures,
        workspace_digest=digest,
        covers_target=(selector is None or selector == FAILING_TEST) if covers is None else covers,
    )


BASELINE = rec(
    "baseline",
    exit_code=1,
    selector=FAILING_TEST,
    digest=START,
    failures=frozenset({FAILING_TEST}),
    source="harness",
)
BASELINE_SUITE = rec(
    "baseline_suite",
    exit_code=1,
    digest=START,
    failures=frozenset({FAILING_TEST, OTHER_FAILURE}),
    source="harness",
)
POST = rec("post", exit_code=0, selector=FAILING_TEST)
SUITE = rec("suite", exit_code=0)


def problems(
    *,
    baseline: EvidenceRecord | None = BASELINE,
    baseline_suite: EvidenceRecord | None = BASELINE_SUITE,
    runs: list[EvidenceRecord] | None = None,
    current: str = CURRENT,
    start: str = START,
) -> list[BundleProblem]:
    return bundle_problems(
        baseline=baseline,
        baseline_suite=baseline_suite,
        runs=[POST, SUITE] if runs is None else runs,
        current_digest=current,
        start_digest=start,
    )


def codes(found: list[BundleProblem]) -> list[str]:
    return [p.code for p in found]


def test_a_complete_bundle_has_no_problems() -> None:
    assert problems() == []


def test_baseline_missing() -> None:
    assert codes(problems(baseline=None)) == ["baseline_missing"]


def test_baseline_not_failing() -> None:
    passing = rec("baseline", exit_code=0, selector=FAILING_TEST, digest=START, source="harness")
    assert codes(problems(baseline=passing)) == ["baseline_not_failing"]


def test_post_missing_when_no_current_run_covers_the_target() -> None:
    only_unrelated = rec("post", exit_code=0, selector="tests/other.py", covers=False)
    assert codes(problems(runs=[only_unrelated])) == ["post_missing", "suite_missing"]
    assert codes(problems(runs=[SUITE])) == []  # a passing whole-suite run covers the target


def test_post_missing_alone() -> None:
    unrelated_suite = rec("suite", exit_code=0, covers=False)  # suite run that does not cover
    assert codes(problems(runs=[unrelated_suite])) == ["post_missing"]


def test_post_failing() -> None:
    failing = rec("post", exit_code=1, selector=FAILING_TEST, failures=frozenset({FAILING_TEST}))
    assert codes(problems(runs=[SUITE, failing])) == ["post_failing"]


def test_suite_missing() -> None:
    assert codes(problems(runs=[POST])) == ["suite_missing"]


def test_suite_new_failures_lists_the_new_ids_in_the_message() -> None:
    new = frozenset({OTHER_FAILURE, "tests/test_new.py::test_regression"})
    failing_suite = rec("suite", exit_code=1, failures=new)
    found = problems(runs=[failing_suite, POST])
    assert codes(found) == ["suite_new_failures"]
    assert "tests/test_new.py::test_regression" in found[0].message
    assert OTHER_FAILURE not in found[0].message  # it was already failing in the baseline


def test_suite_new_failures_message_names_at_most_twenty_ids() -> None:
    many = frozenset(f"tests/test_many.py::test_{i:02}" for i in range(30))
    found = problems(runs=[rec("suite", exit_code=1, failures=many), POST])
    shown = [i for i in many if i in found[0].message]
    assert len(shown) == 20


def test_suite_unparsed_when_it_fails_without_parseable_failures() -> None:
    assert codes(problems(runs=[rec("suite", exit_code=1), POST])) == ["suite_unparsed"]


def test_a_suite_failing_only_with_baseline_failures_is_ok() -> None:
    legacy = rec("suite", exit_code=1, failures=frozenset({OTHER_FAILURE}))
    # the suite run comes first; the later `post` run is the latest that covers the target
    assert problems(runs=[legacy, POST]) == []


def test_diff_empty_when_the_workspace_is_back_at_the_start_digest() -> None:
    runs = [
        rec("post", exit_code=0, selector=FAILING_TEST, digest=START),
        rec("suite", exit_code=0, digest=START),
    ]
    assert codes(problems(runs=runs, current=START)) == ["diff_empty"]


def test_runs_with_a_stale_digest_are_ignored() -> None:
    stale_post = rec("post", exit_code=0, selector=FAILING_TEST, digest=OLD)
    stale_suite = rec("suite", exit_code=0, digest=OLD)
    assert codes(problems(runs=[stale_post, stale_suite])) == ["post_missing", "suite_missing"]


def test_a_stale_run_does_not_hide_a_current_failure() -> None:
    current_fail = rec(
        "post", exit_code=1, selector=FAILING_TEST, failures=frozenset({FAILING_TEST})
    )
    stale_pass = rec("post", exit_code=0, selector=FAILING_TEST, digest=OLD)
    assert codes(problems(runs=[SUITE, stale_pass, current_fail])) == ["post_failing"]


def test_the_latest_current_run_wins_flaky_pass_then_fail() -> None:
    fail = rec("post", exit_code=1, selector=FAILING_TEST, failures=frozenset({FAILING_TEST}))
    assert codes(problems(runs=[SUITE, POST, fail])) == ["post_failing"]


def test_the_latest_current_run_wins_fail_then_pass() -> None:
    fail = rec("post", exit_code=1, selector=FAILING_TEST, failures=frozenset({FAILING_TEST}))
    assert problems(runs=[SUITE, fail, POST]) == []


def test_the_latest_suite_run_wins() -> None:
    bad = rec("suite", exit_code=1, failures=frozenset({"tests/test_new.py::test_x"}))
    assert codes(problems(runs=[bad, POST, SUITE])) == []
    assert codes(problems(runs=[SUITE, bad, POST])) == ["suite_new_failures"]


def test_problems_come_in_the_documented_code_order() -> None:
    found = problems(baseline=None, runs=[], current=START)
    assert codes(found) == ["baseline_missing", "post_missing", "suite_missing", "diff_empty"]
    assert all(p.message for p in found)


def test_without_a_failing_test_the_whole_suite_is_the_target() -> None:
    shared = rec("baseline", exit_code=1, digest=START, failures=frozenset({OTHER_FAILURE}),
                 source="harness")  # fmt: skip
    passing = rec("suite", exit_code=0)
    assert problems(baseline=shared, baseline_suite=shared, runs=[passing]) == []
    failing = rec("suite", exit_code=1, failures=frozenset({OTHER_FAILURE}))
    assert codes(problems(baseline=shared, baseline_suite=shared, runs=[failing])) == [
        "post_failing"
    ]
    assert codes(problems(baseline=shared, baseline_suite=shared, runs=[])) == [
        "post_missing",
        "suite_missing",
    ]
