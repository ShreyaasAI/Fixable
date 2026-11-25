"""M10b §0.4: a provider HTTP 429 waits and retries without using an ordinary attempt."""

from __future__ import annotations

from typing import Any

import httpx
import litellm
import pytest
from m4_fakes import FakeChannel, FakeClock, Step, happy_steps, run_agent
from test_m4_agent import MakeAgent

from swf.agent.web_agent import WebAgentConfig, is_rate_limit, retry_after_seconds

pytest_plugins = ["m4_fakes"]


def rate_limit(retry_after: str | None = None) -> litellm.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        429, headers=headers, request=httpx.Request("POST", "https://api.portkey.ai/v1/chat")
    )
    return litellm.RateLimitError(
        "Rate limit reached for requests", llm_provider="openai", model="m", response=response
    )


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_the_default_429_schedule() -> None:
    assert tuple(WebAgentConfig().rate_limit_backoff_seconds) == (10, 20, 40, 60, 60, 60)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (rate_limit(), True),
        (StatusError(429), True),
        (StatusError(500), False),
        (RuntimeError("boom"), False),
    ],
)
def test_is_rate_limit(exc: Exception, expected: bool) -> None:
    assert is_rate_limit(exc) is expected


@pytest.mark.parametrize(
    ("value", "expected"), [("90", 90.0), ("500", 120.0), ("soon", None), (None, None)]
)
def test_retry_after_is_read_and_capped(value: str | None, expected: float | None) -> None:
    assert retry_after_seconds(rate_limit(value)) == expected
    assert retry_after_seconds(RuntimeError("x")) is None


def test_429s_wait_on_their_own_schedule_and_use_no_ordinary_attempt(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    steps: list[Step] = [rate_limit(), rate_limit(), rate_limit(), rate_limit()]
    steps += happy_steps()
    agent, model, _, channel = make_agent(
        steps, channel=FakeChannel(answers=lambda q: "no"), poll_seconds=1000.0
    )  # one sleep per wait (waits are sliced by poll_seconds)
    result = run_agent(agent)
    assert result["exit_status"] == "Submitted"
    assert channel.asked == []  # four 429s, yet no "retries exhausted" question
    assert fake_clock.sleeps[:4] == [10.0, 20.0, 40.0, 60.0]
    retries = [e for e in channel.events_of("retry") if e["step"] == "model"]
    assert [e["rate_limited"] for e in retries] == [True] * 4
    assert [e["wait_seconds"] for e in retries] == [10.0, 20.0, 40.0, 60.0]


def test_a_longer_retry_after_wins(make_agent: MakeAgent, fake_clock: FakeClock) -> None:
    steps: list[Step] = [rate_limit("90"), *happy_steps()]
    agent, *_ = make_agent(steps, poll_seconds=1000.0)
    assert run_agent(agent)["exit_status"] == "Submitted"
    assert fake_clock.sleeps[0] == 90.0


def test_after_the_429_budget_they_count_as_ordinary_failures(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    steps: list[Step] = [rate_limit() for _ in range(2 + 3)]
    agent, model, _, channel = make_agent(
        steps, channel=FakeChannel(answers=lambda q: "no"), rate_limit_backoff_seconds=(1.0, 2.0)
    )
    result = run_agent(agent)
    assert result["exit_status"] == "RetriesExhausted"
    assert model.queries == 5  # 2 budgeted 429 waits, then the 3 ordinary attempts
    assert fake_clock.sleeps == [1.0, 2.0, 5.0, 15.0]
    assert len(channel.asked) == 1


def test_other_errors_keep_the_ordinary_schedule(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    steps: list[Step] = [StatusError(500)] * 3
    agent, *_, channel = make_agent(steps, channel=FakeChannel(answers=lambda q: "no"))
    assert run_agent(agent)["exit_status"] == "RetriesExhausted"
    assert fake_clock.sleeps == [5.0, 15.0]
    events: list[dict[str, Any]] = channel.events_of("retry")
    assert all(not e.get("rate_limited") for e in events)


def test_stop_is_noticed_during_a_long_429_wait(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    steps: list[Step] = [rate_limit("120"), *happy_steps()]
    agent, model, _, channel = make_agent(steps, poll_seconds=2.0)
    real_sleep = agent.sleep

    def sleep_then_cancel(seconds: float) -> None:
        real_sleep(seconds)
        if sum(fake_clock.sleeps) >= 4.0:
            channel.cancel()

    agent.sleep = sleep_then_cancel
    result = run_agent(agent)
    assert result["exit_status"] == "Stopped"
    assert sum(fake_clock.sleeps) < 10  # stopped within a poll slice, not after 120 s
    assert model.queries == 1


def test_429s_in_between_ordinary_failures_do_not_reset_or_use_attempts(
    make_agent: MakeAgent, fake_clock: FakeClock
) -> None:
    boom = RuntimeError("boom")
    steps: list[Step] = [boom, rate_limit(), boom, rate_limit(), boom]
    agent, model, _, channel = make_agent(steps, channel=FakeChannel(answers=lambda q: "no"))
    assert run_agent(agent)["exit_status"] == "RetriesExhausted"
    assert model.queries == 5
    assert len(channel.asked) == 1  # asked after the third ordinary failure
    ordinary = [e for e in channel.events_of("retry") if not e.get("rate_limited")]
    assert [e["attempt"] for e in ordinary] == [1, 2, 3]


class OddHeaders:
    def __init__(self) -> None:
        self.headers = ["retry-after", "5"]  # no .get


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after-ms": "2500"}, 2.5), ({"retry-after": "inf"}, 120.0), ({}, None)],
)
def test_retry_after_forms(headers: dict[str, str], expected: float | None) -> None:
    exc = litellm.RateLimitError(
        "Rate limit",
        llm_provider="openai",
        model="m",
        response=httpx.Response(
            429, headers=headers, request=httpx.Request("POST", "https://x.test")
        ),
    )
    assert retry_after_seconds(exc) == expected


def test_retry_after_never_crashes_on_odd_headers() -> None:
    exc = RuntimeError("429")
    exc.response = OddHeaders()  # type: ignore[attr-defined]
    assert retry_after_seconds(exc) is None
