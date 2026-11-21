"""`pr_title` and `render_pr_body` (m6-contract §4): rules, sections, caps, no command output."""

from __future__ import annotations

import uuid

from m6_fakes import make_task

from swf.evidence import EvidenceRecord
from swf.pr import PrSummary, pr_title, render_pr_body

SECRET_OUTPUT = "SECRET_OUTPUT token=ghs_abc123 /home/user/.ssh"
TASK_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
SHA = "0123456789abcdef0123456789abcdef01234567"


def rec(kind: str, exit_code: int, failures: tuple[str, ...] = (), **kw: object) -> EvidenceRecord:
    return EvidenceRecord(
        kind=kind,  # type: ignore[arg-type]
        source="harness",
        command=f"python -m pytest -q  # {kind}",
        exit_code=exit_code,
        output=SECRET_OUTPUT,
        failures=frozenset(failures),
        **kw,  # type: ignore[arg-type]
    )


def summary(**overrides: object) -> PrSummary:
    fields: dict[str, object] = {
        "task_id": TASK_ID,
        "description": "Page 2 repeats the last item.",
        "failing_test": "tests/test_pager.py::test_second_page",
        "test_command": "python -m pytest -q",
        "baseline": rec("baseline", 1, ("tests/test_pager.py::test_second_page",)),
        "baseline_suite": rec("baseline_suite", 1, ("tests/test_pager.py::test_second_page",)),
        "post": rec("post", 0),
        "suite": rec("suite", 0),
        "rerun": rec("harness_rerun", 0),
        "files": ["pager.py", "src/a.py"],
        "base_sha": SHA,
    }
    fields.update(overrides)
    return PrSummary(**fields)  # type: ignore[arg-type]


# --- title ----------------------------------------------------------------------------------


def test_title_uses_the_failing_test_when_there_is_one() -> None:
    task = make_task(failing_test="tests/test_pager.py::test_second_page")
    assert pr_title(task) == "SWF: fix tests/test_pager.py::test_second_page"


def test_title_falls_back_to_the_first_line_of_the_description() -> None:
    task = make_task(failing_test=None, description="Fix the pager\nsecond line\nthird")
    assert pr_title(task) == "SWF: Fix the pager"


def test_title_is_one_line_and_at_most_72_chars_with_an_ellipsis() -> None:
    for task in (
        make_task(failing_test="tests/" + "x" * 200 + "::t"),
        make_task(failing_test=None, description="word " * 100),
    ):
        title = pr_title(task)
        assert len(title) <= 72 and "\n" not in title
        assert title.startswith("SWF: ")
        assert title.endswith(("...", "…"))


def test_a_short_title_is_not_ellipsised() -> None:
    assert not pr_title(make_task(failing_test="t::a")).endswith(("...", "…"))


# --- body -----------------------------------------------------------------------------------


def test_body_has_the_sections_in_order_and_the_footer() -> None:
    body = render_pr_body(summary())
    what, evidence, files = body.index("What"), body.index("Evidence"), body.index("Files changed")
    assert what < evidence < files
    assert "Page 2 repeats the last item." in body
    assert "tests/test_pager.py::test_second_page" in body
    assert "pager.py" in body and "src/a.py" in body
    footer = (
        f"Opened by SWF for task {TASK_ID} on base {SHA[:12]}. "
        "The agent never merges; review and merge in GitHub."
    )
    assert body.rstrip().endswith(footer)


def test_body_lists_commands_exit_codes_and_failure_ids() -> None:
    body = render_pr_body(summary())
    assert "python -m pytest -q" in body
    assert "tests/test_pager.py::test_second_page" in body  # baseline failure id
    rows = [ln for ln in body.splitlines() if ln.startswith("|")]
    assert len(rows) >= 2 + 5  # header + separator + five evidence rows
    baseline_row = next(r for r in rows if "baseline" in r.lower() and "suite" not in r.lower())
    assert " 1 " in baseline_row
    post_row = next(r for r in rows if "post" in r.lower() and "suite" not in r.lower())
    assert " 0 " in post_row


def test_body_never_contains_command_output() -> None:
    body = render_pr_body(summary())
    assert "SECRET_OUTPUT" not in body
    assert "ghs_abc123" not in body
    assert ".ssh" not in body


def test_missing_evidence_records_do_not_break_the_body() -> None:
    body = render_pr_body(summary(baseline_suite=None, post=None, suite=None, rerun=None))
    assert "Evidence" in body and "SECRET_OUTPUT" not in body


def test_failure_ids_are_capped_at_20_per_row() -> None:
    ids = tuple(f"tests/t.py::test_{i:03d}" for i in range(60))
    body = render_pr_body(summary(baseline=rec("baseline", 1, ids)))
    assert sum(1 for i in ids if i in body) == 20


def test_file_list_is_capped_at_100_with_a_more_line() -> None:
    files = [f"src/f{i:03d}.py" for i in range(130)]
    body = render_pr_body(summary(files=files))
    assert "src/f099.py" in body and "src/f100.py" not in body
    assert "and 30 more" in body


def test_description_is_cut_to_2000_chars_and_quoted_and_length_is_bounded() -> None:
    huge = "line of a very long description\n" * 20000
    body = render_pr_body(summary(description=huge))
    assert len(body) <= 60000
    assert body.count("line of a very long description") <= 2000 // 31 + 1
    quoted = [ln for ln in body.splitlines() if "line of a very long description" in ln]
    assert quoted and all(ln.startswith(">") for ln in quoted)


def test_body_is_bounded_even_with_a_huge_file_list_and_many_failures() -> None:
    files = [f"deep/dir/{'x' * 80}/file{i}.py" for i in range(5000)]
    ids = tuple(f"tests/{'y' * 200}.py::t{i}" for i in range(500))
    body = render_pr_body(summary(files=files, baseline=rec("baseline", 1, ids)))
    assert len(body) <= 60000
