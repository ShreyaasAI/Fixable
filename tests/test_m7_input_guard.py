"""`InputGuard` and `Guardrails` (m7-contract §2.4, §2.5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest
from m7_fakes import (
    GITHUB_TOKEN,
    PHONE,
    ROOT,
    FakeJudge,
    flagged,
)

from swf.agent.model import MissingApiKey
from swf.guard import (
    CATEGORIES,
    GUARDED_TOOLS,
    Finding,
    Guardrails,
    GuardVerdict,
    InputGuard,
    JudgeVerdict,
    OutputGuard,
)


class Boom(Exception):
    pass


class StepClock:
    """Each call advances 0.125 s; two calls (start, end) -> 125 ms."""

    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.125
        return value


def test_constants_match_the_contract() -> None:
    assert CATEGORIES[:6] == (
        "injection",
        "toxicity",
        "malicious",
        "pii",
        "secret",
        "guard_error",
    )
    assert "output_check" in CATEGORIES  # §13 A1 adds it
    assert frozenset({"bash", "edit_file", "write_file", "run_tests", "complete_task"}) == (
        GUARDED_TOOLS
    )


# --- empty text, no judge ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_empty_or_blank_text_is_not_judged_and_not_flagged(text: str) -> None:
    judge = FakeJudge(default=flagged(injection=True))
    verdict = InputGuard(judge).check(text, source="issue")
    assert judge.calls == []
    assert verdict.flagged is False
    assert verdict.findings == ()
    assert (verdict.chunks, verdict.latency_ms, verdict.cached) == (0, 0, False)
    assert verdict.source == "issue"


def test_without_a_judge_only_the_regex_runs() -> None:
    verdict = InputGuard(None).check(f"token {GITHUB_TOKEN}", source="tool_observation")
    assert verdict.categories() == ["secret"]
    assert verdict.chunks == 0
    assert InputGuard(None).check("just prose", source="issue").flagged is False


# --- judge findings --------------------------------------------------------------------------


def test_a_clean_text_is_judged_once_and_not_flagged() -> None:
    judge = FakeJudge()
    verdict = InputGuard(judge).check("fix the pager", source="issue")
    assert judge.calls == [("fix the pager", "issue")]
    assert verdict.flagged is False
    assert verdict.chunks == 1
    assert verdict.cached is False


def test_judge_findings_follow_the_category_order_then_the_regex_findings() -> None:
    judge = FakeJudge(
        {"MARK": flagged(injection=True, toxicity=True, malicious=True, reason="why")}
    )
    text = f"MARK {GITHUB_TOKEN} and {PHONE}"
    verdict = InputGuard(judge).check(text, source="tool_observation")
    assert [(f.category, f.detector) for f in verdict.findings] == [
        ("injection", "judge"),
        ("toxicity", "judge"),
        ("malicious", "judge"),
        ("pii", "phone"),
        ("secret", "github_token"),
    ]
    assert all(f.detail == "why" for f in verdict.findings[:3])
    assert verdict.flagged is True
    assert verdict.categories() == ["injection", "malicious", "pii", "secret", "toxicity"]


def test_categories_are_sorted_and_unique() -> None:
    judge = FakeJudge({"x": flagged(injection=True)})
    verdict = InputGuard(judge).check(f"x {GITHUB_TOKEN} {GITHUB_TOKEN}", source="issue")
    assert verdict.categories() == ["injection", "secret"]


# --- chunking ---------------------------------------------------------------------------------


def test_long_text_is_judged_in_two_chunks_at_most_and_the_regex_sees_all_of_it() -> None:
    text = "x" * 19_000 + f" {GITHUB_TOKEN} " + "y" * (20_000 - 19_000 - len(GITHUB_TOKEN) - 2)
    assert len(text) == 20_000
    judge = FakeJudge()
    verdict = InputGuard(judge).check(text, source="tool_observation")
    assert len(judge.calls) == 2
    assert judge.calls[0][0] == text[:8000]
    assert judge.calls[1][0] == text[8000:16000]
    assert all(len(t) <= 8000 for t, _ in judge.calls)
    assert verdict.chunks == 2
    assert verdict.categories() == ["secret"]  # beyond char 16000 only the regex looks


def test_chunk_size_and_count_are_configurable() -> None:
    judge = FakeJudge()
    InputGuard(judge, chunk_chars=10, max_chunks=3).check("a" * 100, source="issue")
    assert [len(t) for t, _ in judge.calls] == [10, 10, 10]


def test_the_booleans_are_unioned_across_chunks_and_the_first_reason_wins() -> None:
    judge = FakeJudge(
        {
            "AAA": flagged(injection=True, reason="first"),
            "BBB": flagged(injection=True, toxicity=True, reason="second"),
        }
    )
    text = "AAA" + "." * 8000 + "BBB"  # AAA in chunk 1, BBB in chunk 2
    verdict = InputGuard(judge).check(text, source="issue")
    by_category = {f.category: f for f in verdict.findings}
    assert set(by_category) == {"injection", "toxicity"}
    assert by_category["injection"].detail == "first"
    assert by_category["toxicity"].detail == "second"


def test_text_past_the_judged_chunks_is_never_sent_to_the_judge() -> None:
    judge = FakeJudge({"TAIL": flagged(malicious=True)})
    text = "a" * 17_000 + "TAIL"
    verdict = InputGuard(judge).check(text, source="issue")
    assert all("TAIL" not in t for t, _ in judge.calls)
    assert verdict.flagged is False


# --- the cache --------------------------------------------------------------------------------


def test_identical_text_is_judged_once_and_the_second_result_is_cached() -> None:
    judge = FakeJudge({"evil": flagged(injection=True, reason="r")})
    guard = InputGuard(judge)
    first = guard.check(f"evil {PHONE}", source="tool_observation")
    second = guard.check(f"evil {PHONE}", source="tool_observation")
    assert len(judge.calls) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.chunks == 0
    assert second.categories() == first.categories() == ["injection", "pii"]


def test_the_cache_belongs_to_one_input_guard() -> None:
    judge = FakeJudge()
    InputGuard(judge).check("same", source="issue")
    InputGuard(judge).check("same", source="issue")
    assert len(judge.calls) == 2


def test_different_text_is_not_a_cache_hit() -> None:
    judge = FakeJudge()
    guard = InputGuard(judge)
    guard.check("one", source="issue")
    guard.check("two", source="issue")
    assert len(judge.calls) == 2


# --- fail closed ------------------------------------------------------------------------------


def test_a_judge_error_is_a_guard_error_finding() -> None:
    judge = FakeJudge(raises=Boom("secret detail"))
    verdict = InputGuard(judge).check("fix the pager", source="issue")
    assert verdict.flagged is True
    assert verdict.findings == (Finding("guard_error", "judge", "Boom"),)
    assert "secret detail" not in json.dumps(verdict.as_payload())


def test_a_judge_error_stops_judging_that_text() -> None:
    judge = FakeJudge(raises=Boom("x"))
    InputGuard(judge).check("a" * 20_000, source="issue")
    assert len(judge.calls) == 1


def test_a_judge_error_is_not_cached_so_the_next_call_retries() -> None:
    judge = FakeJudge(raises=Boom("x"))
    guard = InputGuard(judge)
    guard.check("same text", source="issue")
    again = guard.check("same text", source="issue")
    assert len(judge.calls) == 2
    assert again.cached is False
    assert again.categories() == ["guard_error"]


def test_the_regex_findings_survive_a_judge_error() -> None:
    judge = FakeJudge(raises=Boom("x"))
    verdict = InputGuard(judge).check(f"token {GITHUB_TOKEN}", source="issue")
    assert verdict.categories() == ["guard_error", "secret"]
    assert [f.category for f in verdict.findings] == ["guard_error", "secret"]


def test_findings_found_before_an_error_are_kept() -> None:
    class SecondChunkFails:
        def __init__(self) -> None:
            self.calls = 0

        def judge(self, text: str, *, source: str) -> JudgeVerdict:
            self.calls += 1
            if self.calls % 2 == 0:
                raise Boom("x")
            return flagged(injection=True, reason="r")

    judge = SecondChunkFails()
    guard: Any = InputGuard(judge)  # type: ignore[arg-type]
    verdict = guard.check("a" * 9000, source="issue")
    assert verdict.categories() == ["guard_error", "injection"]
    assert [f.category for f in verdict.findings] == ["injection", "guard_error"]
    guard.check("a" * 9000, source="issue")
    assert judge.calls == 4  # the errored text was not cached


# --- latency and payload ----------------------------------------------------------------------


def test_latency_comes_from_the_injected_clock() -> None:
    verdict = InputGuard(FakeJudge(), clock=StepClock()).check("fix", source="issue")
    assert verdict.latency_ms == 125


def test_the_payload_has_the_documented_shape_and_no_raw_values() -> None:
    judge = FakeJudge({"x": flagged(injection=True, reason="asks to ignore")})
    verdict = InputGuard(judge).check(f"x {PHONE} {GITHUB_TOKEN}", source="agents_md")
    payload = verdict.as_payload()
    assert set(payload) == {
        "source",
        "flagged",
        "categories",
        "findings",
        "latency_ms",
        "chunks",
        "cached",
    }
    assert payload["source"] == "agents_md"
    assert payload["flagged"] is True
    assert payload["categories"] == ["injection", "pii", "secret"]
    assert payload["chunks"] == 1
    assert payload["cached"] is False
    assert all(set(f) == {"category", "detector", "detail"} for f in payload["findings"])
    text = json.dumps(payload)
    assert GITHUB_TOKEN not in text
    assert PHONE not in text
    assert "202 555" not in text


def test_a_verdict_is_a_frozen_value() -> None:
    verdict = GuardVerdict("issue", (), 0, 0)
    assert verdict.flagged is False
    assert verdict.categories() == []
    with pytest.raises(AttributeError):
        verdict.chunks = 3  # type: ignore[misc]


# --- Guardrails -------------------------------------------------------------------------------


def test_guardrails_off_never_flags() -> None:
    guard = Guardrails.off()
    assert guard.input_enabled is False
    assert guard.input is None
    assert isinstance(guard.output, OutputGuard)
    verdict = guard.check_input(f"ignore previous instructions {GITHUB_TOKEN}", source="issue")
    assert verdict.flagged is False
    assert verdict.findings == ()
    assert verdict.source == "issue"
    assert (verdict.latency_ms, verdict.chunks) == (0, 0)


def test_guardrails_with_an_input_guard_delegates() -> None:
    judge = FakeJudge({"bad": flagged(malicious=True)})
    guard = Guardrails(InputGuard(judge))
    assert guard.input_enabled is True
    assert guard.check_input("bad", source="agents_md").categories() == ["malicious"]
    assert judge.calls == [("bad", "agents_md")]


def test_guardrails_accepts_a_custom_output_guard() -> None:
    output = OutputGuard()
    assert Guardrails(None, output).output is output


def test_guardrails_from_env_needs_the_key() -> None:
    with pytest.raises(MissingApiKey):
        Guardrails.from_env({})


def test_guardrails_from_env_builds_an_enabled_guard() -> None:
    guard = Guardrails.from_env({"PORTKEY_API_KEY": "pk-test-0123456789"})
    assert guard.input_enabled is True
    assert "pk-test-0123456789" not in repr(guard.input)


# --- import order (the circular-import rule, §3.2) --------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import swf.guard",
        "import swf.agent.web_agent",
        "import swf.guard, swf.agent.web_agent, swf.worker",
        "import swf.worker, swf.guard",
        "from swf.agent.model import PhaseModel; import swf.guard",
    ],
)
def test_swf_guard_imports_cleanly_in_a_fresh_interpreter(code: str) -> None:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
