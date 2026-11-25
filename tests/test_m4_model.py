"""`PhaseModel` (m4-contract §5.2): per-phase wire model and tools, Portkey kwargs, parsing."""

from __future__ import annotations

import json
from typing import Any

import pytest
from m4_fakes import FakeCompletion, text_response, tool_calls_response, tool_response
from minisweagent.exceptions import FormatError

from swf.agent.model import (
    DEFAULT_PHASE_MODELS,
    PORTKEY_BASE_URL,
    MissingApiKey,
    PhaseModel,
)
from swf.agent.tools import tool_schemas

KEY = "pk-test-secret-0123456789"
ENV = {"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "off"}  # m10a §1.8
MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "task", "extra": {"internal": True}},
]


def make(*responses: Any, env: dict[str, str] | None = None) -> tuple[PhaseModel, FakeCompletion]:
    fake = FakeCompletion(*(responses or (tool_response("bash", {"command": "ls"}),)))
    return PhaseModel(completion=fake, env=ENV if env is None else env), fake


def test_defaults_match_the_contract() -> None:
    assert DEFAULT_PHASE_MODELS == {
        "investigate": "@baidu-ernie/baidu/ernie-5.5-flash-preview:free",
        "plan": "@baidu-ernie/baidu/ernie-5.5-flash-preview:free",
        "execute": "@glm-4-7-flash/glm-4.7-flash",
        "verify": "@glm-4-7-flash/glm-4.7-flash",
    }
    assert PORTKEY_BASE_URL == "https://api.portkey.ai/v1"


def test_missing_api_key_is_an_error() -> None:
    with pytest.raises(MissingApiKey):
        PhaseModel(completion=FakeCompletion(text_response("x")), env={})


def test_starts_in_the_investigate_phase() -> None:
    model, _ = make()
    assert model.phase == "investigate"
    assert model.current_model == "@baidu-ernie/baidu/ernie-5.5-flash-preview:free"


@pytest.mark.parametrize(
    ("phase", "wire"),
    [
        ("investigate", "openai/@baidu-ernie/baidu/ernie-5.5-flash-preview:free"),
        ("plan", "openai/@baidu-ernie/baidu/ernie-5.5-flash-preview:free"),
        ("execute", "openai/@glm-4-7-flash/glm-4.7-flash"),
        ("verify", "openai/@glm-4-7-flash/glm-4.7-flash"),
    ],
)
def test_each_phase_uses_its_wire_model_and_its_own_tools(phase: str, wire: str) -> None:
    model, fake = make()
    model.set_phase(phase)
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["model"] == wire
    assert sent["tools"] == tool_schemas(phase)
    assert model.phase == phase


def test_the_tool_list_changes_with_the_phase() -> None:
    model, fake = make()
    model.query(MESSAGES)
    model.set_phase("execute")
    model.query(MESSAGES)
    model.set_phase("verify")
    model.query(MESSAGES)
    tool_names = [{t["function"]["name"] for t in c["tools"]} for c in fake.calls]
    assert "submit_plan" in tool_names[0]
    assert "edit_file" not in tool_names[0]
    assert "edit_file" in tool_names[1]
    assert "submit_plan" not in tool_names[1]
    assert "edit_file" not in tool_names[2]
    assert {"record_evidence", "complete_task"} <= tool_names[2]


def test_portkey_gateway_arguments() -> None:
    model, fake = make()
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["api_base"] == PORTKEY_BASE_URL
    assert sent["api_key"] == KEY
    assert sent["extra_headers"] == {"x-portkey-api-key": KEY}


def test_internal_message_fields_are_not_sent() -> None:
    model, fake = make()
    model.query(MESSAGES)
    assert all("extra" not in m for m in fake.calls[0]["messages"])
    assert [m["role"] for m in fake.calls[0]["messages"]] == ["system", "user"]


def test_the_key_never_reaches_config_or_serialization() -> None:
    model, fake = make()
    message = model.query(MESSAGES)
    assert KEY not in model.config.model_dump_json()
    assert KEY not in json.dumps(model.serialize())
    assert KEY not in json.dumps(message, default=str)
    assert KEY not in json.dumps(model.get_template_vars(), default=str)


def test_a_bash_tool_call_becomes_an_action() -> None:
    model, _ = make(tool_response("bash", {"command": "ls -la"}, id="call_9"))
    action = model.query(MESSAGES)["extra"]["actions"][0]
    assert action["tool"] == "bash"
    assert action["command"] == "ls -la"
    assert action["args"] == {"command": "ls -la"}
    assert action["tool_call_id"] == "call_9"


def test_other_tools_keep_name_arguments_and_id() -> None:
    model, _ = make(tool_response("read_file", {"path": "a.py", "start_line": 2}, id="call_2"))
    (action,) = model.query(MESSAGES)["extra"]["actions"]
    assert action["tool"] == "read_file"
    assert action["args"] == {"path": "a.py", "start_line": 2}
    assert action["tool_call_id"] == "call_2"


def test_several_tool_calls_keep_their_order() -> None:
    response = tool_calls_response(
        [
            ("list_dir", {}, "c1"),
            ("search", {"pattern": "x"}, "c2"),
            ("bash", {"command": "pwd"}, "c3"),
        ]
    )
    model, _ = make(response)
    actions = model.query(MESSAGES)["extra"]["actions"]
    assert [(a["tool"], a["tool_call_id"]) for a in actions] == [
        ("list_dir", "c1"),
        ("search", "c2"),
        ("bash", "c3"),
    ]


def test_a_tool_that_is_not_allowed_in_the_phase_still_parses() -> None:
    # The phase gate is the agent's job (it refuses and tells the model); parsing accepts it.
    model, _ = make(tool_response("edit_file", {"path": "a", "old": "b", "new": "c"}))
    (action,) = model.query(MESSAGES)["extra"]["actions"]
    assert action["tool"] == "edit_file"


@pytest.mark.parametrize(
    "response",
    [
        text_response("I will just talk."),
        tool_response("rm_rf_everything", {"x": 1}),
        tool_response("bash", raw_arguments="{not json"),
        tool_response("bash", raw_arguments="[1, 2]"),
        tool_response("bash", raw_arguments='"a string"'),
    ],
    ids=["no-tool-call", "unknown-tool", "bad-json", "json-list", "json-string"],
)
def test_unusable_responses_raise_format_error(response: Any) -> None:
    model, _ = make(response)
    with pytest.raises(FormatError) as raised:
        model.query(MESSAGES)
    assert raised.value.messages[0]["role"] == "user"
    assert raised.value.messages[0]["content"]


def test_a_free_model_is_recorded_with_zero_cost() -> None:
    model, _ = make()
    assert model.query(MESSAGES)["extra"]["cost"] == 0.0


def test_the_message_records_which_model_and_phase_answered() -> None:
    model, _ = make()
    first = model.query(MESSAGES)
    assert first["extra"]["phase"] == "investigate"
    assert "baidu/ernie-5.5-flash-preview:free" in first["extra"]["model"]
    model.set_phase("execute")
    second = model.query(MESSAGES)
    assert second["extra"]["phase"] == "execute"
    assert "glm-4-7-flash/glm-4.7-flash" in second["extra"]["model"]


def test_one_message_history_is_shared_across_the_model_switch() -> None:
    """RQ1, offline: the second model receives the first model's whole conversation."""
    model, fake = make()
    history = [*MESSAGES]
    reply = model.query(history)
    history.append({k: v for k, v in reply.items() if k != "extra"} | {"extra": reply["extra"]})
    history.append({"role": "tool", "tool_call_id": "call_1", "content": "ok"})
    model.set_phase("execute")
    model.query(history)
    first, second = fake.calls
    assert first["model"] != second["model"]
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert len(second["messages"]) == len(first["messages"]) + 2


def test_from_env_uses_the_defaults() -> None:
    model = PhaseModel.from_env(ENV)
    assert model.current_model == "@baidu-ernie/baidu/ernie-5.5-flash-preview:free"
    model.set_phase("verify")
    assert model.current_model == "@glm-4-7-flash/glm-4.7-flash"


def test_from_env_applies_the_model_overrides() -> None:
    env = {**ENV, "SWF_MODEL_INVESTIGATE": "@acme/inv-1", "SWF_MODEL_EXECUTE": "@acme/exe-1"}
    model = PhaseModel.from_env(env)
    seen = {}
    for phase in ("investigate", "plan", "execute", "verify"):
        model.set_phase(phase)
        seen[phase] = model.current_model
    assert seen == {
        "investigate": "@acme/inv-1",
        "plan": "@acme/inv-1",
        "execute": "@acme/exe-1",
        "verify": "@acme/exe-1",
    }


def test_from_env_without_a_key_fails() -> None:
    with pytest.raises(MissingApiKey):
        PhaseModel.from_env({})


def test_there_is_no_hidden_upstream_retry() -> None:
    """swf._mswea sets MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1; WebAgent owns model retries."""
    model, fake = make(RuntimeError("gateway down"))
    with pytest.raises(RuntimeError, match="gateway down"):
        model.query(MESSAGES)
    assert len(fake.calls) == 1
