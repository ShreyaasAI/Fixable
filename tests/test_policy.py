"""Command classifier + phase policy (plan M3, §12; spec §8).

DoD: 0 forbidden bypasses on the adversarial corpus; unparseable -> approval.
The sandbox remains the real boundary; the classifier decides what needs a human first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from swf.policy import (
    AGENT_PHASES,
    TOOL_PHASES,
    Verdict,
    check_tool,
    classify,
)

CORPUS_PATH = Path(__file__).parent / "corpus" / "policy_commands.yaml"
CORPUS: list[dict[str, Any]] = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
DEFAULT_ALLOW = ["python -m pytest*"]


def _run(entry: dict[str, Any]) -> Verdict:
    return classify(
        entry["cmd"],
        phase=entry.get("phase", "execute"),
        allow=entry.get("allow", DEFAULT_ALLOW),
    )


def test_corpus_is_large_and_well_formed() -> None:
    assert len(CORPUS) >= 200
    for e in CORPUS:
        assert set(e) <= {"cmd", "expect", "phase", "allow"}, e
        assert e["expect"] in {"allow", "approve", "forbid"}, e
    assert sum(e["expect"] == "forbid" for e in CORPUS) >= 100


@pytest.mark.parametrize(
    "entry", CORPUS, ids=[f"{i:03d}-{e['expect']}" for i, e in enumerate(CORPUS)]
)
def test_corpus_entry(entry: dict[str, Any]) -> None:
    verdict = _run(entry)
    assert verdict.decision == entry["expect"], (entry["cmd"], verdict)
    assert verdict.reasons or verdict.decision == "allow"


def test_dod_zero_forbidden_bypasses() -> None:
    bypasses = [
        e["cmd"] for e in CORPUS if e["expect"] == "forbid" and _run(e).decision != "forbid"
    ]
    assert bypasses == []


def test_never_more_permissive_than_expected() -> None:
    rank = {"allow": 0, "approve": 1, "forbid": 2}
    looser = [e["cmd"] for e in CORPUS if rank[_run(e).decision] < rank[e["expect"]]]
    assert looser == []


@pytest.mark.parametrize("cmd", ["ls 'x", "echo $(", "if then fi", "ls )(", "", "\n"])
def test_unparseable_needs_approval(cmd: str) -> None:
    v = classify(cmd)
    assert v.decision == "approve"
    assert any("parse" in r or "empty" in r for r in v.reasons)


def test_agents_md_allow_cannot_override_forbidden() -> None:
    for cmd in ["rm -rf build", "git push", "sudo ls", "docker ps", "nc x 1"]:
        assert classify(cmd, allow=["*", cmd, cmd + "*"]).decision == "forbid", cmd


def test_allow_patterns_apply_per_simple_command_not_to_chains() -> None:
    # A pattern that would match the whole string textually must not allow a second command.
    assert (
        classify("python -m pytest -q; curl https://pypi.org", allow=["python -m pytest*"]).decision
        == "approve"
    )
    assert classify("python -m pytest -q $(id)", allow=["python -m pytest*"]).decision == "approve"


def test_allow_pattern_is_anchored() -> None:
    assert (
        classify("echo python -m pytest", allow=["python -m pytest*"]).decision == "allow"
    )  # echo
    assert classify("xpython -m pytest", allow=["python -m pytest*"]).decision == "approve"


def test_reasons_explain_forbid() -> None:
    v = classify("ls && rm -rf build")
    assert v.decision == "forbid"
    assert any("rm" in r for r in v.reasons)


def test_unknown_phase_rejected() -> None:
    with pytest.raises(ValueError):
        classify("ls", phase="deploy")


# --- tool gate (plan §12 table) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "allowed_phases"),
    [
        ("bash", {"investigate", "execute", "verify"}),
        ("read_file", {"investigate", "execute", "verify"}),
        ("list_dir", {"investigate", "execute", "verify"}),
        ("search", {"investigate", "execute", "verify"}),
        ("run_tests", {"investigate", "execute", "verify"}),
        ("ask_user", {"investigate", "execute", "verify"}),
        ("edit_file", {"execute"}),
        ("write_file", {"execute"}),
        ("submit_plan", {"investigate"}),
        ("record_evidence", {"verify"}),
        ("complete_task", {"verify"}),
    ],
)
def test_tool_phase_table(tool: str, allowed_phases: set[str]) -> None:
    assert TOOL_PHASES[tool] == frozenset(allowed_phases)
    for phase in AGENT_PHASES:
        v = check_tool(tool, phase)
        assert v.decision == ("allow" if phase in allowed_phases else "forbid"), (tool, phase)


def test_unknown_tool_forbidden() -> None:
    assert check_tool("delete_repo", "execute").decision == "forbid"


def test_non_agent_phase_forbids_every_tool() -> None:
    for tool in TOOL_PHASES:
        assert check_tool(tool, "pr").decision == "forbid"
