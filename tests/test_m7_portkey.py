"""Portkey saved guardrail, opt-in (m7-contract §13 A1): `portkey_hook_verdicts` and the
`x-portkey-config` header of `PhaseModel`. No network: `FakeCompletion` stands in for litellm."""

from __future__ import annotations

import json
from typing import Any

import pytest
from m4_fakes import FakeCompletion, tool_response

from swf.agent.model import PhaseModel, PhaseModelConfig
from swf.guard import portkey_hook_verdicts
from swf.settings import SettingsError

KEY = "pk-test-secret-0123456789"
SLUG = "pc-swf-m7-d1cf5c"
GUARDRAIL_ID = "pg-swf-m7-c8f3fc"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "task"}]


def hook_results(*, regex: bool = True, schema: bool = True) -> dict[str, Any]:
    """The shape of the 2026-11-21 spike response (contract §13 A1)."""
    return {
        "after_request_hooks": [
            {
                "id": GUARDRAIL_ID,
                "verdict": regex and schema,
                "checks": [
                    {
                        "id": "default.regexMatch",
                        "verdict": regex,
                        "data": {"matchedText": "ok", "textExcerpt": '{"ok": true}'},
                    },
                    {
                        "id": "default.jsonSchema",
                        "verdict": schema,
                        "data": {"textExcerpt": '{"ok": true}', "explanation": "SECRET-EXPLAIN"},
                    },
                ],
            }
        ]
    }


def spike_response(**kw: bool) -> dict[str, Any]:
    return {"id": "chatcmpl-1", "model": "glm", "hook_results": hook_results(**kw)}


# --- portkey_hook_verdicts ---------------------------------------------------------------------


def test_a_passing_hook_has_no_failed_checks() -> None:
    assert portkey_hook_verdicts(spike_response()) == [
        {"id": GUARDRAIL_ID, "verdict": True, "failed_checks": []}
    ]


def test_a_failing_check_is_listed_by_id() -> None:
    assert portkey_hook_verdicts(spike_response(schema=False)) == [
        {"id": GUARDRAIL_ID, "verdict": False, "failed_checks": ["default.jsonSchema"]}
    ]


def test_every_failed_check_is_listed_in_order() -> None:
    assert portkey_hook_verdicts(spike_response(regex=False, schema=False)) == [
        {
            "id": GUARDRAIL_ID,
            "verdict": False,
            "failed_checks": ["default.regexMatch", "default.jsonSchema"],
        }
    ]


def test_several_hooks_give_several_entries() -> None:
    response = spike_response()
    response["hook_results"]["after_request_hooks"].append(
        {"id": "pg-other", "verdict": False, "checks": [{"id": "x.y", "verdict": False}]}
    )
    assert portkey_hook_verdicts(response) == [
        {"id": GUARDRAIL_ID, "verdict": True, "failed_checks": []},
        {"id": "pg-other", "verdict": False, "failed_checks": ["x.y"]},
    ]


def test_a_hook_without_checks_has_no_failed_checks() -> None:
    response = {"hook_results": {"after_request_hooks": [{"id": "pg-a", "verdict": False}]}}
    assert portkey_hook_verdicts(response) == [
        {"id": "pg-a", "verdict": False, "failed_checks": []}
    ]


@pytest.mark.parametrize(
    "response",
    [None, "text", 5, ["hook_results"], {}, {"id": "x"}, {"hook_results": {}}],
    ids=["none", "str", "int", "list", "empty", "no-hook-key", "no-after-request-hooks"],
)
def test_a_missing_key_or_a_non_dict_gives_none(response: Any) -> None:
    assert portkey_hook_verdicts(response) is None


def test_the_result_never_copies_data_matched_text_or_excerpts() -> None:
    text = json.dumps(portkey_hook_verdicts(spike_response(schema=False)))
    for forbidden in ("data", "matchedText", "textExcerpt", "SECRET-EXPLAIN", '{"ok": true}'):
        assert forbidden not in text
    (entry,) = portkey_hook_verdicts(spike_response())  # type: ignore[misc]
    assert set(entry) == {"id", "verdict", "failed_checks"}


def test_the_input_is_not_mutated() -> None:
    response = spike_response(schema=False)
    before = json.dumps(response, sort_keys=True)
    portkey_hook_verdicts(response)
    assert json.dumps(response, sort_keys=True) == before


# --- the header -------------------------------------------------------------------------------


def make(*, env: dict[str, str], response: Any = None) -> tuple[PhaseModel, FakeCompletion]:
    fake = FakeCompletion(
        response if response is not None else tool_response("bash", {"command": "ls"})
    )
    return PhaseModel.from_env(env, completion=fake), fake


def test_the_config_slug_is_sent_as_a_header() -> None:
    model, fake = make(env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG})
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["extra_headers"] == {"x-portkey-api-key": KEY, "x-portkey-config": SLUG}


def test_with_off_there_is_no_config_header() -> None:  # m10a §1.8: the variable is required
    model, fake = make(env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "off"})
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["extra_headers"] == {"x-portkey-api-key": KEY}


def test_the_config_can_also_be_given_directly() -> None:
    fake = FakeCompletion(tool_response("bash", {"command": "ls"}))
    model = PhaseModel(completion=fake, env={"PORTKEY_API_KEY": KEY}, portkey_config=SLUG)
    model.query(MESSAGES)
    assert fake.calls[0]["extra_headers"]["x-portkey-config"] == SLUG


def test_the_config_defaults_to_off() -> None:
    assert PhaseModelConfig().portkey_config is None


@pytest.mark.parametrize(
    "slug",
    [
        "swf-m7",
        "pc-",
        "pc-bad slug",
        "pc-" + "a" * 65,
        "PC-abc",
        "pc-swf_m7",
        "pc-swf/m7",
        "pg-abc",
    ],
)
def test_an_invalid_slug_is_a_settings_error(slug: str) -> None:  # m10a §1.8
    with pytest.raises(SettingsError):
        PhaseModel.from_env({"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": slug})


@pytest.mark.parametrize("slug", ["pc-a", "pc-swf-m7-d1cf5c", "pc-" + "A1-" * 21 + "z"])
def test_a_valid_slug_is_accepted(slug: str) -> None:
    model, fake = make(env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": slug})
    model.query(MESSAGES)
    assert fake.calls[0]["extra_headers"]["x-portkey-config"] == slug


def test_the_key_is_not_in_the_error_for_a_bad_slug() -> None:
    with pytest.raises(SettingsError) as info:
        PhaseModel.from_env({"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "nope"})
    assert KEY not in str(info.value)


# --- the verdicts reach the message ---------------------------------------------------------------


def with_hooks(**kw: bool) -> Any:
    response = tool_response("bash", {"command": "ls"})
    response.hook_results = hook_results(**kw)
    return response


def test_with_a_config_the_message_carries_the_hook_verdicts() -> None:
    model, _ = make(
        env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG}, response=with_hooks(schema=False)
    )
    message = model.query(MESSAGES)
    assert message["extra"]["portkey_hooks"] == [
        {"id": GUARDRAIL_ID, "verdict": False, "failed_checks": ["default.jsonSchema"]}
    ]


def test_with_a_config_and_a_passing_hook_the_verdict_is_true() -> None:
    model, _ = make(env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG}, response=with_hooks())
    message = model.query(MESSAGES)
    assert message["extra"]["portkey_hooks"] == [
        {"id": GUARDRAIL_ID, "verdict": True, "failed_checks": []}
    ]


def test_with_a_config_but_no_hook_results_the_value_is_none() -> None:
    model, _ = make(env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG})
    message = model.query(MESSAGES)
    assert "portkey_hooks" in message["extra"]
    assert message["extra"]["portkey_hooks"] is None  # the agent turns this into a guard_error


def test_without_a_config_the_message_has_no_portkey_hooks_key() -> None:
    model, _ = make(
        env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "off"}, response=with_hooks()
    )
    message = model.query(MESSAGES)
    assert "portkey_hooks" not in message["extra"]


def test_the_raw_hook_data_is_not_copied_into_the_verdicts() -> None:
    model, _ = make(
        env={"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG}, response=with_hooks(schema=False)
    )
    message = model.query(MESSAGES)
    text = json.dumps(message["extra"]["portkey_hooks"])
    assert "matchedText" not in text
    assert "SECRET-EXPLAIN" not in text
    assert KEY not in text
