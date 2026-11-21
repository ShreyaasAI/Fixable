"""M7 live proof of the Portkey saved guardrail (m7-contract §13 A1).

Opt-in: needs `PORTKEY_API_KEY` (free models, $0; the key is never printed or stored). The saved
config `pc-swf-m7-d1cf5c` carries the guardrail `pg-swf-m7-c8f3fc` (regexMatch "ok" + a JSON
schema requiring `ok`). Run:

    PYTHONDONTWRITEBYTECODE=1 uv run --env-file .env pytest -o addopts="" -q -rs -m live \
        tests/test_m7_live_portkey.py

`PhaseModel.query` needs a tool call, so a plain-text `{"ok": true}` reply cannot go through it.
The decisive test therefore drives `PhaseModel` with a completion hook that drops `tools` (a text
reply) and keeps the real response: it proves the header is sent, `hook_results` survives litellm's
`ModelResponse.model_dump()`, and `portkey_hook_verdicts` reads it. A second test runs the normal
tool-calling `query` and checks the shape of `extra["portkey_hooks"]`.
"""

from __future__ import annotations

import json
import os
from typing import Any

import litellm
import pytest
from minisweagent.exceptions import FormatError

from swf.agent.model import PhaseModel
from swf.guard import portkey_hook_verdicts

pytestmark = [
    pytest.mark.live,
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("PORTKEY_API_KEY"), reason="PORTKEY_API_KEY is not set (live models)"
    ),
]

CONFIG_SLUG = "pc-swf-m7-d1cf5c"
GUARDRAIL_ID = "pg-swf-m7-c8f3fc"
ASK = 'Reply with exactly this JSON and nothing else: {"ok": true}'


def live_env() -> dict[str, str]:
    return {"PORTKEY_API_KEY": os.environ["PORTKEY_API_KEY"], "SWF_PORTKEY_CONFIG": CONFIG_SLUG}


def test_hook_results_survive_the_litellm_response_and_the_verdict_is_true() -> None:
    sent: list[dict[str, Any]] = []
    responses: list[Any] = []

    def text_only_completion(**kwargs: Any) -> Any:
        sent.append({k: v for k, v in kwargs.items() if k not in ("api_key", "extra_headers")})
        sent[-1]["x-portkey-config"] = kwargs["extra_headers"].get("x-portkey-config")
        kwargs.pop("tools", None)  # a text reply, so the guardrail sees {"ok": true}
        kwargs["max_tokens"] = 1000  # reasoning models need room before the answer
        response = litellm.completion(**kwargs)
        responses.append(response)
        return response

    model = PhaseModel.from_env(live_env(), completion=text_only_completion)
    model.set_phase("execute")  # glm-4.7-flash, the model of the spike
    with pytest.raises(FormatError):  # no tool call in a text reply
        model.query([{"role": "user", "content": ASK}])
    (response,) = responses
    assert sent[0]["x-portkey-config"] == CONFIG_SLUG
    dumped = response.model_dump()
    assert "hook_results" in dumped, "litellm dropped hook_results: keep SWF_PORTKEY_CONFIG off"
    verdicts = portkey_hook_verdicts(dumped)
    assert verdicts == [{"id": GUARDRAIL_ID, "verdict": True, "failed_checks": []}]
    assert os.environ["PORTKEY_API_KEY"] not in json.dumps(verdicts)


def test_a_tool_calling_query_reports_portkey_hooks_in_extra() -> None:
    model = PhaseModel.from_env(live_env())
    model.set_phase("execute")
    messages = [
        {
            "role": "user",
            "content": "Call the bash tool with the command `echo ok`. Do not answer in text.",
        }
    ]
    try:
        message = model.query(messages)
    except FormatError:
        pytest.skip("the model answered without a tool call (flaky model, not a guard defect)")
    hooks = message["extra"]["portkey_hooks"]
    assert isinstance(hooks, list)
    assert len(hooks) == 1
    assert hooks[0]["id"] == GUARDRAIL_ID
    assert isinstance(hooks[0]["verdict"], bool)
    assert isinstance(hooks[0]["failed_checks"], list)
    assert set(hooks[0]) == {"id", "verdict", "failed_checks"}  # never `data` or `matchedText`
    assert os.environ["PORTKEY_API_KEY"] not in json.dumps(message["extra"].get("portkey_hooks"))
