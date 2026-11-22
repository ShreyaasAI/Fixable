"""Agent tools (m4-contract §5.1, amended by §12 A1/A3): schemas, path rules, container tools."""

from __future__ import annotations

import shlex
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from m4_fakes import FakeEnv, call
from pydantic import ValidationError as PydanticValidationError

from swf import policy
from swf.agent.tools import (
    TOOL_SCHEMAS,
    Plan,
    ToolEnv,
    ToolError,
    resolve_repo_path,
    run_tool,
    tool_schemas,
)

pytest_plugins = ["m4_fakes"]

TOOLS = {
    "bash",
    "read_file",
    "list_dir",
    "search",
    "edit_file",
    "write_file",
    "run_tests",
    "submit_plan",
    "ask_user",
    "record_evidence",
    "complete_task",
}

# The M3 phase table, spelled out (independent of `policy.TOOL_PHASES`).
M3_TABLE: dict[str, list[str]] = {
    "investigate": [
        "bash", "read_file", "list_dir", "search", "run_tests", "ask_user", "submit_plan",
    ],
    "execute": [
        "bash", "read_file", "list_dir", "search", "edit_file", "write_file", "run_tests",
        "ask_user",
    ],
    "verify": [
        "bash", "read_file", "list_dir", "search", "run_tests", "ask_user", "record_evidence",
        "complete_task",
    ],
}  # fmt: skip

PLAN: dict[str, Any] = {
    "summary": "s",
    "root_cause": "r",
    "steps": "one",
    "files": ["a.py"],
    "test_plan": "t",
}


def names(phase: str) -> list[str]:
    return [t["function"]["name"] for t in tool_schemas(phase)]


# --- schemas -------------------------------------------------------------------------------


def test_start_verify_is_not_a_tool() -> None:
    assert "start_verify" not in TOOL_SCHEMAS
    assert "start_verify" not in policy.TOOL_PHASES


def test_the_tool_set_is_the_contract_set() -> None:
    assert set(TOOL_SCHEMAS) == TOOLS
    assert set(policy.TOOL_PHASES) == TOOLS


@pytest.mark.parametrize("phase", ["investigate", "execute", "verify"])
def test_tool_schemas_match_the_m3_table_exactly(phase: str) -> None:
    assert sorted(names(phase)) == sorted(M3_TABLE[phase])
    assert names(phase) == [t for t in TOOL_SCHEMAS if t in M3_TABLE[phase]]  # TOOL_SCHEMAS order


@pytest.mark.parametrize("phase", policy.PHASES)
def test_tool_schemas_agree_with_policy_for_every_phase(phase: str) -> None:
    assert names(phase) == [t for t in TOOL_SCHEMAS if phase in policy.TOOL_PHASES[t]]
    for tool in names(phase):
        assert policy.check_tool(tool, phase).decision == "allow"


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_schema_is_a_valid_closed_function_schema(tool: str) -> None:
    schema = TOOL_SCHEMAS[tool]
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == tool
    assert function["description"]
    parameters = function["parameters"]
    Draft202012Validator.check_schema(parameters)
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False


def valid(tool: str, args: dict[str, Any]) -> bool:
    try:
        Draft202012Validator(TOOL_SCHEMAS[tool]["function"]["parameters"]).validate(args)
    except ValidationError:
        return False
    return True


VALID_ARGS: list[tuple[str, dict[str, Any]]] = [
    ("bash", {"command": "ls"}),
    ("bash", {"command": "ls", "timeout_seconds": 600}),
    ("bash", {"command": "x" * 20000, "timeout_seconds": 1}),
    ("read_file", {"path": "a.py"}),
    ("read_file", {"path": "a.py", "start_line": 1, "end_line": 20}),
    ("list_dir", {}),
    ("list_dir", {"path": "tests"}),
    ("search", {"pattern": "def "}),
    ("search", {"pattern": "x" * 500, "path": "src", "glob": "*.py"}),
    ("edit_file", {"path": "a.py", "old": "a", "new": ""}),
    ("edit_file", {"path": "a.py", "old": "a", "new": "b", "replace_all": True}),
    ("write_file", {"path": "a.py", "content": ""}),
    ("run_tests", {}),
    ("run_tests", {"selector": "tests/test_a.py::test_b[param-1]"}),
    ("submit_plan", PLAN),
    ("ask_user", {"question": "which size?"}),
    ("record_evidence", {"kind": "baseline", "note": "n"}),
    ("record_evidence", {"kind": "harness_rerun", "note": "n", "command": "pytest"}),
    ("complete_task", {"summary": "done"}),
]
INVALID_ARGS: list[tuple[str, dict[str, Any]]] = [
    ("bash", {}),
    ("bash", {"command": ""}),
    ("bash", {"command": "x" * 20001}),
    ("bash", {"command": "ls", "timeout_seconds": 0}),
    ("bash", {"command": "ls", "timeout_seconds": 601}),
    ("bash", {"command": "ls", "extra": 1}),
    ("read_file", {}),
    ("read_file", {"path": "a", "start_line": 0}),
    ("read_file", {"path": "a", "end_line": 0}),
    ("list_dir", {"extra": 1}),
    ("search", {}),
    ("search", {"pattern": ""}),
    ("search", {"pattern": "x" * 501}),
    ("edit_file", {"path": "a", "new": "b"}),
    ("edit_file", {"path": "a", "old": "", "new": "b"}),
    ("edit_file", {"path": "a", "old": "a"}),
    ("write_file", {"path": "a"}),
    ("run_tests", {"selector": ""}),
    ("run_tests", {"selector": "a b"}),
    ("run_tests", {"selector": "a;rm -rf /"}),
    ("run_tests", {"selector": "$(id)"}),
    ("run_tests", {"selector": "x" * 301}),
    ("run_tests", {"extra": 1}),
    ("submit_plan", {**PLAN, "steps": ""}),
    ("submit_plan", {**PLAN, "steps": ["one"]}),
    ("submit_plan", {**PLAN, "files": ["f"] * 51}),
    ("submit_plan", {**PLAN, "summary": ""}),
    ("submit_plan", {**PLAN, "summary": "x" * 2001}),
    ("submit_plan", {k: v for k, v in PLAN.items() if k != "test_plan"}),
    ("ask_user", {"question": ""}),
    ("ask_user", {"question": "x" * 2001}),
    ("record_evidence", {"kind": "nonsense", "note": "n"}),
    ("record_evidence", {"kind": "post"}),
    ("complete_task", {"summary": ""}),
    ("complete_task", {"summary": "x" * 4001}),
]


@pytest.mark.parametrize(("tool", "args"), VALID_ARGS)
def test_schema_accepts_valid_arguments(tool: str, args: dict[str, Any]) -> None:
    assert valid(tool, args)


@pytest.mark.parametrize(("tool", "args"), INVALID_ARGS)
def test_schema_rejects_invalid_arguments(tool: str, args: dict[str, Any]) -> None:
    assert not valid(tool, args)


# --- Plan ----------------------------------------------------------------------------------


def test_plan_accepts_the_contract_fields() -> None:
    plan = Plan(**PLAN)
    assert plan.steps == ["one"]
    assert Plan(**{**PLAN, "files": []}).files == []


@pytest.mark.parametrize(
    "bad",
    [
        {"steps": ""},
        {"steps": "\n".join(["s"] * 21)},
        {"files": ["f"] * 51},
        {"summary": ""},
        {"root_cause": "x" * 2001},
        {"test_plan": ""},
        {"unexpected": 1},
    ],
)
def test_plan_rejects_violations(bad: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        Plan(**{**PLAN, **bad})


# --- resolve_repo_path ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("a/b.py", "/repo/a/b.py"),
        ("/repo/a/b.py", "/repo/a/b.py"),
        ("./a/../b", "/repo/b"),
        ("a//b", "/repo/a/b"),
        (".", "/repo"),
        ("/repo", "/repo"),
        ("dir with space/f x.txt", "/repo/dir with space/f x.txt"),
        ("~/x", "/repo/~/x"),  # `~` is just a directory name, never the home directory
    ],
)
def test_resolve_repo_path_accepts(given: str, expected: str) -> None:
    assert resolve_repo_path(given) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "../x", "a/../../x", "/etc/passwd", "/repo/../etc", "/repoevil/x", "a\x00b", "/"],
)
def test_resolve_repo_path_rejects(bad: str) -> None:
    with pytest.raises(ToolError):
        resolve_repo_path(bad)


# --- run_tool (container tools other than bash) --------------------------------------------


def go(
    env: FakeEnv, tool: str, *, test_command: str | None = "python -m pytest -q", **args: Any
) -> dict[str, Any]:
    return run_tool(env, call(tool, **args), test_command=test_command, output_limit=16000)


def test_fake_env_satisfies_the_tool_env_protocol(fake_env: FakeEnv) -> None:
    assert isinstance(fake_env, ToolEnv)


def test_result_shape(fake_env: FakeEnv) -> None:
    result = go(fake_env, "read_file", path="pager.py")
    assert set(result) >= {"output", "returncode", "exception_info"}
    assert result["returncode"] == 0
    assert "def paginate" in result["output"]


def test_read_file_numbers_lines_and_honours_a_range(fake_env: FakeEnv) -> None:
    (fake_env.root / "words.txt").write_text("alpha\nbravo\ncharlie\ndelta\n", encoding="utf-8")
    whole = go(fake_env, "read_file", path="words.txt")["output"].splitlines()
    assert "1" in whole[0]
    assert "alpha" in whole[0]
    assert "charlie" in whole[2]
    part = go(fake_env, "read_file", path="words.txt", start_line=2, end_line=3)["output"]
    assert "bravo" in part
    assert "charlie" in part
    assert "alpha" not in part
    assert "delta" not in part
    assert "2" in part.splitlines()[0]


def test_read_file_is_capped_at_256_kib(fake_env: FakeEnv) -> None:
    (fake_env.root / "big.txt").write_text("x" * 300_000, encoding="utf-8")
    output = go(fake_env, "read_file", path="big.txt")["output"]
    assert output.count("x") <= 256 * 1024
    assert output.count("x") >= 200_000  # truncated, not refused
    assert len(output) <= 256 * 1024 + 1024


def test_read_file_missing_or_escaping_path_is_a_tool_error(fake_env: FakeEnv) -> None:
    with pytest.raises(ToolError):
        go(fake_env, "read_file", path="nope.py")
    with pytest.raises(ToolError):
        go(fake_env, "read_file", path="../outside.txt")


def test_list_dir_marks_directories(fake_env: FakeEnv) -> None:
    lines = go(fake_env, "list_dir")["output"].splitlines()
    assert "pager.py" in lines
    assert "tests/" in lines
    sub = go(fake_env, "list_dir", path="tests")["output"].splitlines()
    assert any(name.startswith("test_pager") for name in sub)
    with pytest.raises(ToolError):
        go(fake_env, "list_dir", path="missing")


def test_search_finds_matches_and_caps_at_200_lines(fake_env: FakeEnv) -> None:
    found = go(fake_env, "search", pattern="def paginate")["output"]
    assert "pager.py" in found
    assert "def paginate" in found
    (fake_env.root / "many.txt").write_text("hit\n" * 500, encoding="utf-8")
    capped = go(fake_env, "search", pattern="hit", glob="many.txt")["output"]
    matches = [line for line in capped.splitlines() if line.startswith("/repo/many.txt:")]
    assert 0 < len(matches) <= 200


def test_edit_file_replaces_a_unique_match(fake_env: FakeEnv) -> None:
    result = go(fake_env, "edit_file", path="pager.py", old="- 1 if page > 1 else 0", new="")
    assert result["returncode"] == 0
    assert "- 1 if" not in (fake_env.root / "pager.py").read_text(encoding="utf-8")


def test_edit_file_without_a_match_fails_and_leaves_the_file(fake_env: FakeEnv) -> None:
    before = (fake_env.root / "pager.py").read_bytes()
    with pytest.raises(ToolError):
        go(fake_env, "edit_file", path="pager.py", old="not in the file", new="x")
    assert (fake_env.root / "pager.py").read_bytes() == before


def test_edit_file_with_several_matches_needs_replace_all(fake_env: FakeEnv) -> None:
    target = fake_env.root / "dup.txt"
    target.write_text("a\nb a\na\n", encoding="utf-8")
    with pytest.raises(ToolError):
        go(fake_env, "edit_file", path="dup.txt", old="a", new="z")
    assert target.read_text(encoding="utf-8") == "a\nb a\na\n"
    result = go(fake_env, "edit_file", path="dup.txt", old="a", new="z", replace_all=True)
    assert result["returncode"] == 0
    assert target.read_text(encoding="utf-8") == "z\nb z\nz\n"


def test_edit_file_refuses_paths_outside_the_repo(fake_env: FakeEnv) -> None:
    with pytest.raises(ToolError):
        go(fake_env, "edit_file", path="../x.py", old="a", new="b")


def test_write_file_creates_directories_and_writes_utf8(fake_env: FakeEnv) -> None:
    result = go(fake_env, "write_file", path="new/dir/f.txt", content="héllo\n")
    assert result["returncode"] == 0
    assert (fake_env.root / "new" / "dir" / "f.txt").read_bytes() == "héllo\n".encode()


def test_write_file_is_capped_at_1_mib_of_utf8(fake_env: FakeEnv) -> None:
    ok = go(fake_env, "write_file", path="ok.txt", content="a" * (1024 * 1024))
    assert ok["returncode"] == 0
    assert (fake_env.root / "ok.txt").stat().st_size == 1024 * 1024
    with pytest.raises(ToolError):
        go(fake_env, "write_file", path="big.txt", content="a" * (1024 * 1024 + 1))
    assert not (fake_env.root / "big.txt").exists()
    # the cap counts UTF-8 bytes, not characters
    with pytest.raises(ToolError):
        go(fake_env, "write_file", path="wide.txt", content="é" * (600 * 1024))
    assert not (fake_env.root / "wide.txt").exists()


def test_run_tests_runs_the_agents_md_test_command(fake_env: FakeEnv) -> None:
    fake_env._bash = {"python -m pytest -q": ("2 passed", 0)}
    result = go(fake_env, "run_tests")
    assert fake_env.executed == ["python -m pytest -q"]
    assert result["returncode"] == 0
    assert "2 passed" in result["output"]


def test_run_tests_appends_the_selector_shell_quoted(fake_env: FakeEnv) -> None:
    selector = "tests/test_pager.py::test_second_page[a=1]"
    go(fake_env, "run_tests", selector=selector)
    (command,) = fake_env.executed
    assert command.startswith("python -m pytest -q ")
    assert command.endswith(shlex.quote(selector))
    assert shlex.split(command)[-1] == selector


def test_run_tests_propagates_a_failing_return_code(fake_env: FakeEnv) -> None:
    fake_env._bash = lambda cmd: {"output": "1 failed", "returncode": 1, "exception_info": ""}
    assert go(fake_env, "run_tests")["returncode"] == 1


@pytest.mark.parametrize("selector", ["a b", "x;rm -rf /", "$(id)", "a`id`", "a|b", "", "x" * 301])
def test_run_tests_rejects_a_selector_that_is_not_plain(fake_env: FakeEnv, selector: str) -> None:
    with pytest.raises(ToolError):
        go(fake_env, "run_tests", selector=selector)
    assert fake_env.executed == []


def test_run_tests_without_a_test_command_is_a_tool_error(fake_env: FakeEnv) -> None:
    with pytest.raises(ToolError, match="no test command configured in AGENTS.md"):
        go(fake_env, "run_tests", test_command=None)
    assert fake_env.executed == []


def test_read_only_tools_leave_the_tree_alone(fake_env: FakeEnv) -> None:
    before = {p: p.read_bytes() for p in fake_env.root.rglob("*") if p.is_file()}
    go(fake_env, "read_file", path="pager.py")
    go(fake_env, "list_dir")
    go(fake_env, "search", pattern="def")
    after = {p: p.read_bytes() for p in fake_env.root.rglob("*") if p.is_file()}
    assert before == after
