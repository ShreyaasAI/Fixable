"""Per-phase model through Portkey's OpenAI-compatible gateway (m4-contract §5.2).

One `PhaseModel` holds one message history for the whole task; `set_phase` switches the wire
model and the tool list: investigate/plan -> ernie-5.5-flash-preview:free (OpenRouter),
execute/verify -> glm-4.7-flash (z-ai); Portkey Model Catalog slugs verified 2026-11-20.
Calls go through litellm as `openai/<wire model>` against `api_base`. The API key is read at
init and kept off `config`, so `serialize()` and the saved trajectory never contain it.
Free models have no litellm price, so cost tracking ignores errors (cost 0).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

import litellm
from jinja2 import StrictUndefined, Template
from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig

from swf.agent.tools import TOOL_SCHEMAS, tool_schemas

DEFAULT_PHASE_MODELS: dict[str, str] = {
    "investigate": "@baidu-ernie/baidu/ernie-5.5-flash-preview:free",
    "plan": "@baidu-ernie/baidu/ernie-5.5-flash-preview:free",
    "execute": "@glm-4-7-flash/glm-4.7-flash",
    "verify": "@glm-4-7-flash/glm-4.7-flash",
}
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"
# Catalog slugs have no litellm price; without this every cost lookup prints a provider banner.
litellm.suppress_debug_info = True


class PhaseModelConfig(LitellmModelConfig):
    model_name: str = DEFAULT_PHASE_MODELS["investigate"]
    phase_models: dict[str, str] = dict(DEFAULT_PHASE_MODELS)
    api_base: str = PORTKEY_BASE_URL
    api_key_env: str = "PORTKEY_API_KEY"
    cost_tracking: Literal["default", "ignore_errors"] = "ignore_errors"


class MissingApiKey(Exception):
    pass


class AgentModel(Protocol):
    """mini-swe-agent's `Model` protocol plus the phase switch."""

    config: Any

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]: ...

    def format_message(self, **kwargs: Any) -> dict[str, Any]: ...

    def format_observation_messages(
        self,
        message: dict[str, Any],
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]: ...

    def serialize(self) -> dict[str, Any]: ...

    def set_phase(self, phase: str) -> None: ...


def _format_error(template: str, error: str, finish_reason: Any, has_tool_calls: bool) -> None:
    raise FormatError(
        {
            "role": "user",
            "content": Template(template, undefined=StrictUndefined).render(
                error=error,
                actions=[],
                has_tool_calls=has_tool_calls,
                finish_reason=finish_reason,
            ),
            "extra": {"interrupt_type": "FormatError"},
        }
    )


class PhaseModel(LitellmModel):
    def __init__(
        self,
        *,
        completion: Callable[..., Any] = litellm.completion,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("config_class", PhaseModelConfig)
        super().__init__(**kwargs)
        source = os.environ if env is None else env
        key = source.get(self.config.api_key_env) or ""
        if not key:
            raise MissingApiKey(f"{self.config.api_key_env} is not set")
        self._api_key = key  # never on self.config: config is serialised into the trajectory
        self._completion = completion
        self.phase = "investigate"
        self.config.model_name = self.current_model

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> PhaseModel:
        """Defaults plus `SWF_MODEL_INVESTIGATE` (investigate + plan) and `SWF_MODEL_EXECUTE`
        (execute + verify) overrides; the key comes from `PORTKEY_API_KEY`."""
        source = os.environ if env is None else env
        models = dict(DEFAULT_PHASE_MODELS)
        if investigate := source.get("SWF_MODEL_INVESTIGATE"):
            models["investigate"] = models["plan"] = investigate
        if execute := source.get("SWF_MODEL_EXECUTE"):
            models["execute"] = models["verify"] = execute
        return cls(env=source, phase_models=models, model_name=models["investigate"], **kwargs)

    @property
    def current_model(self) -> str:
        return str(self.config.phase_models[self.phase])

    def set_phase(self, phase: str) -> None:
        if phase not in self.config.phase_models:
            raise ValueError(f"no model configured for phase {phase!r}")
        self.phase = phase
        self.config.model_name = self.current_model

    def _query(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self._completion(
            model="openai/" + self.current_model,
            messages=messages,
            tools=tool_schemas(self.phase),
            api_base=self.config.api_base,
            api_key=self._api_key,
            extra_headers={"x-portkey-api-key": self._api_key},
            **(self.config.model_kwargs | kwargs),
        )

    def _parse_actions(self, response: Any) -> list[dict[str, Any]]:
        """Tool calls -> `{"tool", "args", "tool_call_id"}` (+ `"command"` for bash)."""
        choice = response.choices[0]
        template = self.config.format_error_template
        finish = getattr(choice, "finish_reason", None)
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        if not tool_calls:
            _format_error(
                template,
                "No tool calls found in the response. Every response MUST include at least one "
                "tool call.",
                finish,
                False,
            )
        actions: list[dict[str, Any]] = []
        for call in tool_calls:
            name = call.function.name
            error = ""
            args: Any = {}
            try:
                args = json.loads(call.function.arguments or "{}")
            except Exception as exc:
                error = f"Error parsing tool call arguments: {exc}."
            if not error and not isinstance(args, dict):
                error = "Tool call arguments must be a JSON object."
            if name not in TOOL_SCHEMAS:
                error += f" Unknown tool '{name}'."
            elif name == "bash" and isinstance(args, dict) and "command" not in args:
                error += " Missing 'command' argument in bash tool call."
            if error:
                _format_error(template, error.strip(), finish, True)
            action: dict[str, Any] = {"tool": name, "args": args, "tool_call_id": call.id}
            if name == "bash":
                action["command"] = args["command"]
            actions.append(action)
        return actions

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        message: dict[str, Any] = super().query(messages, **kwargs)
        message.setdefault("extra", {})
        message["extra"]["model"] = self.current_model
        message["extra"]["phase"] = self.phase
        return message
