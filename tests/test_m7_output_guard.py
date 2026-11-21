"""`OutputGuard` (m7-contract §2.5, §0.6): malformed calls are refused, secrets in args flag."""

from __future__ import annotations

import json
from typing import Any

import pytest
from m7_fakes import AWS_KEY, GITHUB_TOKEN, PHONE, SK_KEY

from swf.guard import OutputGuard, OutputVerdict

guard = OutputGuard()

MALFORMED = [
    ("unknown tool", "rm_everything", {}),
    ("args a list", "bash", ["ls"]),
    ("args a string", "bash", "ls"),
    ("args none", "bash", None),
    ("unknown arg", "bash", {"command": "ls", "shell": "zsh"}),
    ("missing required", "bash", {}),
    ("missing required path", "write_file", {"content": "x"}),
    ("wrong type", "bash", {"command": 5}),
    ("wrong type bool", "edit_file", {"path": "a", "old": "b", "new": "c", "replace_all": "yes"}),
    ("empty command", "bash", {"command": ""}),
    ("NUL in content", "write_file", {"path": "a.py", "content": "x\x00y"}),
    ("NUL in command", "bash", {"command": "ls\x00 -la"}),
]


@pytest.mark.parametrize(("name", "tool", "args"), MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_calls_are_refused(name: str, tool: str, args: Any) -> None:
    verdict = guard.check(tool, args)
    assert verdict.decision == "malformed", verdict
    assert verdict.errors
    assert all(isinstance(e, str) and e for e in verdict.errors)
    assert verdict.findings == ()


def test_a_nul_character_is_called_out() -> None:
    verdict = guard.check("write_file", {"path": "a.py", "content": "x\x00y"})
    assert verdict.errors == ("NUL character in arguments",)


def test_a_nul_wins_over_a_secret() -> None:
    verdict = guard.check("write_file", {"path": "a.py", "content": f"\x00 {GITHUB_TOKEN}"})
    assert verdict.decision == "malformed"
    assert verdict.findings == ()


def test_schema_violations_report_the_validator_message() -> None:
    verdict = guard.check("bash", {"command": "ls", "shell": "zsh"})
    assert any("shell" in e for e in verdict.errors)
    unknown = guard.check("nope", {})
    assert any("nope" in e for e in unknown.errors)


FLAGGED = [
    (
        "github token in write_file",
        "write_file",
        {"path": "c.py", "content": f"T = '{GITHUB_TOKEN}'"},
        "github_token",
    ),
    (
        "aws key in bash",
        "bash",
        {"command": f"export AWS_ACCESS_KEY_ID={AWS_KEY} && env"},
        "aws_access_key",
    ),
    (
        "sk key in edit_file",
        "edit_file",
        {"path": "c.py", "old": "KEY = ''", "new": f"KEY = '{SK_KEY}'"},
        "sk_api_key",
    ),
]


@pytest.mark.parametrize(("name", "tool", "args", "detector"), FLAGGED, ids=[f[0] for f in FLAGGED])
def test_a_secret_in_a_string_argument_flags_the_call(
    name: str, tool: str, args: dict[str, Any], detector: str
) -> None:
    verdict = guard.check(tool, args)
    assert verdict.decision == "flag"
    assert verdict.errors == ()
    assert [(f.category, f.detector) for f in verdict.findings] == [("secret", detector)]


def test_findings_are_deduplicated_by_detector_and_counts_are_summed() -> None:
    verdict = guard.check(
        "edit_file",
        {"path": "c.py", "old": f"A = '{AWS_KEY}'", "new": f"A = '{AWS_KEY}'; B = '{AWS_KEY}'"},
    )
    assert verdict.decision == "flag"
    (finding,) = verdict.findings
    assert finding.detector == "aws_access_key"
    assert finding.detail == "3 matches"


def test_two_detectors_give_two_findings() -> None:
    verdict = guard.check("write_file", {"path": "c.py", "content": f"{AWS_KEY}\n{GITHUB_TOKEN}\n"})
    assert {f.detector for f in verdict.findings} == {"aws_access_key", "github_token"}


def test_the_payload_never_holds_the_value() -> None:
    verdict = guard.check("write_file", {"path": "c.py", "content": GITHUB_TOKEN})
    payload = verdict.as_payload()
    assert set(payload) == {"decision", "errors", "findings"}
    assert payload["decision"] == "flag"
    assert payload["errors"] == []
    assert GITHUB_TOKEN not in json.dumps(payload)


OK = [
    ("normal edit", "edit_file", {"path": "pager.py", "old": "a - 1", "new": "a"}),
    ("email in content", "write_file", {"path": "c.py", "content": "OWNER = 'alice@corp.test'\n"}),
    ("phone in content", "write_file", {"path": "c.py", "content": f"PHONE = '{PHONE}'\n"}),
    ("plain command", "bash", {"command": "ls -la"}),
    ("read_file", "read_file", {"path": "pager.py"}),
    ("run_tests", "run_tests", {}),
    ("run_tests selector", "run_tests", {"selector": "tests/test_pager.py::test_second_page"}),
]


@pytest.mark.parametrize(("name", "tool", "args"), OK, ids=[o[0] for o in OK])
def test_normal_calls_are_ok(name: str, tool: str, args: dict[str, Any]) -> None:
    verdict = guard.check(tool, args)
    assert verdict == OutputVerdict("ok")
    assert verdict.as_payload() == {"decision": "ok", "errors": [], "findings": []}


def _plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "summary": "s",
        "root_cause": "r",
        "steps": ["edit pager.py"],
        "files": ["pager.py"],
        "test_plan": "t",
    }
    return plan | over


@pytest.mark.parametrize("field", ["steps", "files"])
def test_a_secret_inside_a_list_argument_flags_the_call(field: str) -> None:
    verdict = guard.check("submit_plan", _plan(**{field: ["ok", f"use {GITHUB_TOKEN}"]}))
    assert verdict.decision == "flag"
    assert [f.detector for f in verdict.findings] == ["github_token"]


def test_a_nul_inside_a_list_argument_is_malformed() -> None:
    verdict = guard.check("submit_plan", _plan(steps=["a\x00b"]))
    assert verdict.decision == "malformed"
    assert verdict.errors == ("NUL character in arguments",)


def test_string_collection_recurses_into_nested_dicts_and_lists() -> None:
    from swf.guard import _strings

    nested = {"a": {"b": [f"x{GITHUB_TOKEN}", {"c": "deep"}]}, "d": 5, "e": "top"}
    assert sorted(_strings(nested)) == sorted([f"x{GITHUB_TOKEN}", "deep", "top"])
