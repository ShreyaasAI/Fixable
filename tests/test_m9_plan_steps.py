"""M9 T11 fix: glm emits array-of-string args as bare text, so `steps` is a string on the wire."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from swf.agent.tools import TOOL_SCHEMAS, Plan, ToolError, validate_args

BASE: dict[str, Any] = {
    "summary": "s",
    "root_cause": "r",
    "files": ["pager.py"],
    "test_plan": "t",
}


def plan(steps: Any) -> dict[str, Any]:
    return {**BASE, "steps": steps}


def test_the_schema_types_steps_as_a_string() -> None:
    steps = TOOL_SCHEMAS["submit_plan"]["function"]["parameters"]["properties"]["steps"]
    assert steps["type"] == "string"
    assert steps["maxLength"] == 4000


def test_validate_args_accepts_a_steps_string() -> None:
    args = plan("1. edit\n2. run tests")
    assert validate_args("submit_plan", args) == args


@pytest.mark.parametrize("steps", [["a"], "", "x" * 4001])
def test_validate_args_rejects_other_steps_shapes(steps: Any) -> None:
    with pytest.raises(ToolError):
        validate_args("submit_plan", plan(steps))


def test_plan_splits_strips_and_drops_empty_lines() -> None:
    parsed = Plan.model_validate(plan("1. edit pager.py\n\n  2. run tests  \n"))
    assert parsed.steps == ["1. edit pager.py", "2. run tests"]


@pytest.mark.parametrize("steps", ["   \n  ", "\n".join(["s"] * 21)])
def test_plan_rejects_no_steps_and_too_many(steps: str) -> None:
    with pytest.raises(PydanticValidationError):
        Plan.model_validate(plan(steps))


def test_plan_accepts_twenty_lines_and_a_list() -> None:
    assert len(Plan.model_validate(plan("\n".join(["s"] * 20))).steps) == 20
    assert Plan.model_validate(plan(["a", "b"])).steps == ["a", "b"]


def test_the_approval_payload_keeps_steps_a_list() -> None:
    dumped = Plan.model_validate(plan("a\nb")).model_dump()
    assert dumped["steps"] == ["a", "b"]
    assert isinstance(dumped["steps"], list)
