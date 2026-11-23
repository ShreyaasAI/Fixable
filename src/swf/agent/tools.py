"""Agent tool schemas and the container file tools (m4-contract §5.1).

The model sees OpenAI function schemas (`tool_schemas(phase)` = the tools `policy.check_tool`
allows in that phase). An action is `{"tool", "args", "tool_call_id"}` (bash actions also carry
`"command"`). `validate_args` checks args against the schema; `run_tool` runs the container
tools other than bash over a `ToolEnv` (the sandbox, or a fake in tests). Paths are resolved
against `/repo` here, before they reach the environment.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from swf import policy

READ_FILE_CAP_BYTES = 256 * 1024
WRITE_FILE_CAP_BYTES = 1024 * 1024
SEARCH_MAX_RESULTS = 200
REPO_ROOT = "/repo"
SELECTOR_RE = re.compile(r"^[A-Za-z0-9_./:\[\]=-]{1,300}$")

BACKEND_TOOLS = frozenset({"submit_plan", "ask_user", "record_evidence", "complete_task"})
CONTAINER_TOOLS = frozenset(
    {"read_file", "list_dir", "search", "edit_file", "write_file", "run_tests"}
)


class ToolError(Exception):
    """A tool refused or failed; the message becomes the observation (returncode 1)."""


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _str(description: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **kw}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bash": _fn(
        "bash",
        "Run a bash command in the repository container (cwd /repo). Commands are checked by "
        "a policy: some need the user's approval, some are forbidden.",
        {
            "command": _str("The bash command to run.", minLength=1, maxLength=20000),
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout in seconds (default 300).",
                "minimum": 1,
                "maximum": 600,
            },
        },
        ["command"],
    ),
    "read_file": _fn(
        "read_file",
        "Read a file in the repository, with line numbers (at most 256 KiB).",
        {
            "path": _str("Path relative to the repository root (or under /repo)."),
            "start_line": {"type": "integer", "minimum": 1, "description": "First line (1-based)."},
            "end_line": {"type": "integer", "minimum": 1, "description": "Last line (inclusive)."},
        },
        ["path"],
    ),
    "list_dir": _fn(
        "list_dir",
        "List a directory in the repository (directories end with /).",
        {"path": _str("Directory relative to the repository root (default '.').")},
        [],
    ),
    "search": _fn(
        "search",
        "Search file contents with an extended regular expression (grep -rnE); at most 200 "
        "matching lines.",
        {
            "pattern": _str("Extended regular expression.", minLength=1, maxLength=500),
            "path": _str("Directory or file to search (default '.')."),
            "glob": _str("Only search files whose name matches this glob, e.g. '*.py'."),
        },
        ["pattern"],
    ),
    "edit_file": _fn(
        "edit_file",
        "Replace an exact string in a file. `old` must occur exactly once unless replace_all.",
        {
            "path": _str("File to edit."),
            "old": _str("Exact text to replace.", minLength=1),
            "new": _str("Replacement text."),
            "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
        },
        ["path", "old", "new"],
    ),
    "write_file": _fn(
        "write_file",
        "Create or overwrite a file with the given content (at most 1 MiB).",
        {"path": _str("File to write."), "content": _str("Full file content.")},
        ["path", "content"],
    ),
    "run_tests": _fn(
        "run_tests",
        "Run the repository's test command from AGENTS.md, optionally limited to a selector "
        "(e.g. tests/test_x.py::test_y).",
        {
            "selector": _str(
                "Test selector appended to the test command.", pattern=SELECTOR_RE.pattern
            )
        },
        [],
    ),
    "submit_plan": _fn(
        "submit_plan",
        "Submit your fix plan for the user's approval. You may edit files only after approval.",
        {
            "summary": _str("What you will change.", minLength=1, maxLength=2000),
            "root_cause": _str("Why the test fails.", minLength=1, maxLength=2000),
            # A string, not an array: glm-4.7-flash writes string arrays as bare text (m9 §13 A2).
            "steps": _str("Ordered steps, one per line.", minLength=1, maxLength=4000),
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 50,
                "description": "Files you expect to change.",
            },
            "test_plan": _str("How you will verify the fix.", minLength=1, maxLength=2000),
        },
        ["summary", "root_cause", "steps", "files", "test_plan"],
    ),
    "ask_user": _fn(
        "ask_user",
        "Ask the user a question and wait for the answer. Use only when you are blocked.",
        {"question": _str("The question.", minLength=1, maxLength=2000)},
        ["question"],
    ),
    "record_evidence": _fn(
        "record_evidence",
        "Record evidence that the fix works (e.g. the passing target test).",
        {
            "kind": {
                "type": "string",
                "enum": ["baseline", "post", "suite", "harness_rerun", "diff"],
                "description": "Evidence kind.",
            },
            "note": _str("What this evidence shows."),
            "command": _str("The command that produced it."),
        },
        ["kind", "note"],
    ),
    "complete_task": _fn(
        "complete_task",
        "Finish the task with a summary of the fix and the evidence.",
        {"summary": _str("Summary of the change and evidence.", minLength=1, maxLength=4000)},
        ["summary"],
    ),
}


def tool_schemas(phase: str) -> list[dict[str, Any]]:
    """Schemas of the tools allowed in `phase`, in `TOOL_SCHEMAS` order."""
    return [
        s for name, s in TOOL_SCHEMAS.items() if policy.check_tool(name, phase).decision == "allow"
    ]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    root_cause: str = Field(min_length=1, max_length=2000)
    steps: list[str] = Field(min_length=1, max_length=20)
    files: list[str] = Field(default_factory=list, max_length=50)
    test_plan: str = Field(min_length=1, max_length=2000)

    @field_validator("steps", mode="before")
    @classmethod
    def _split_steps(cls, value: Any) -> Any:
        """The wire form is one step per line; blank lines are dropped."""
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return value


# --- argument validation (the subset of JSON Schema the schemas above use) ------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _check(value: Any, schema: dict[str, Any], where: str) -> None:
    kind = schema.get("type")
    if kind is not None:
        ok = isinstance(value, _TYPES[kind])
        if kind == "integer" and isinstance(value, bool):
            ok = False
        if not ok:
            raise ToolError(f"{where} must be a {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{where} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ToolError(f"{where} must not be empty")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError(f"{where} is longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ToolError(f"{where} does not match {schema['pattern']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{where} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{where} must be <= {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ToolError(f"{where} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError(f"{where} allows at most {schema['maxItems']} items")
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                _check(item, items, f"{where}[{i}]")


def validate_args(tool: str, args: Any) -> dict[str, Any]:
    """Check `args` against the tool's schema; returns them, raises `ToolError`."""
    schema = TOOL_SCHEMAS.get(tool)
    if schema is None:
        raise ToolError(f"unknown tool {tool!r}")
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    params = schema["function"]["parameters"]
    props: dict[str, Any] = params["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(f"unknown argument(s) for {tool}: {', '.join(unknown)}")
    missing = [k for k in params["required"] if k not in args]
    if missing:
        raise ToolError(f"missing argument(s) for {tool}: {', '.join(missing)}")
    for key, value in args.items():
        _check(value, props[key], key)
    return args


# --- paths and the environment -------------------------------------------------------------


def resolve_repo_path(path: str) -> str:
    """Relative or `/repo/...` -> normalised absolute `/repo/...`; refuses escapes and NUL."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ToolError("invalid path")
    candidate = path if path.startswith("/") else f"{REPO_ROOT}/{path}"
    resolved = posixpath.normpath(candidate)
    if resolved != REPO_ROOT and not resolved.startswith(REPO_ROOT + "/"):
        raise ToolError(f"path {path!r} is outside the repository")
    return resolved


@runtime_checkable
class ToolEnv(Protocol):
    """What the agent needs from its environment. Paths passed in are already resolved."""

    def execute(
        self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]: ...

    def read_bytes(self, path: str) -> bytes: ...

    def write_bytes(self, path: str, data: bytes) -> None: ...

    def list_dir(self, path: str) -> list[str]: ...

    def search(self, pattern: str, path: str, glob: str | None, max_results: int) -> list[str]: ...

    def cleanup(self) -> None: ...

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]: ...

    def serialize(self) -> dict[str, Any]: ...


def _ok(output: str) -> dict[str, Any]:
    return {"output": output, "returncode": 0, "exception_info": ""}


def _read_file(env: ToolEnv, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_repo_path(args["path"])
    start = args.get("start_line")
    end = args.get("end_line")
    if start is not None and end is not None and end < start:
        raise ToolError("end_line must be >= start_line")
    text = env.read_bytes(path).decode("utf-8", "replace")
    lines = text.splitlines()
    first = start or 1
    last = min(end or len(lines), len(lines))
    width = len(str(max(last, 1)))
    numbered = "".join(f"{n:>{width}}\t{lines[n - 1]}\n" for n in range(first, last + 1))
    encoded = numbered.encode("utf-8")
    if len(encoded) > READ_FILE_CAP_BYTES:
        note = (
            f"\n[truncated at {READ_FILE_CAP_BYTES} bytes; use start_line/end_line to read the "
            "rest]\n"
        )
        keep = encoded[: READ_FILE_CAP_BYTES - len(note.encode("utf-8"))]
        numbered = keep.decode("utf-8", "ignore") + note
    return _ok(numbered)


def _list_dir(env: ToolEnv, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_repo_path(args.get("path", "."))
    return _ok("\n".join(env.list_dir(path)))


def _search(env: ToolEnv, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_repo_path(args.get("path", "."))
    found = env.search(args["pattern"], path, args.get("glob"), SEARCH_MAX_RESULTS)
    shown = found[:SEARCH_MAX_RESULTS]  # at most 200 lines; the model narrows the search
    return _ok("\n".join(shown) if shown else "no matches")


def _edit_file(env: ToolEnv, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_repo_path(args["path"])
    old: str = args["old"]
    new: str = args["new"]
    try:
        text = env.read_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{path} is not UTF-8 text") from exc
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old text not found in {path}")
    if count > 1 and not args.get("replace_all", False):
        raise ToolError(
            f"old text occurs {count} times in {path}; add context to make it unique or set "
            "replace_all"
        )
    updated = (
        text.replace(old, new) if args.get("replace_all", False) else text.replace(old, new, 1)
    )
    env.write_bytes(path, updated.encode("utf-8"))
    n = count if args.get("replace_all", False) else 1
    return _ok(f"edited {path} ({n} replacement{'s' if n != 1 else ''})")


def _write_file(env: ToolEnv, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_repo_path(args["path"])
    data = args["content"].encode("utf-8")
    if len(data) > WRITE_FILE_CAP_BYTES:
        raise ToolError(f"content is {len(data)} bytes; the limit is {WRITE_FILE_CAP_BYTES}")
    env.write_bytes(path, data)
    return _ok(f"wrote {len(data)} bytes to {path}")


def build_test_command(test_command: str | None, selector: str | None) -> str:
    """The AGENTS.md test command with an optional (validated, shell-quoted) selector."""
    if not test_command:
        raise ToolError("no test command configured in AGENTS.md")
    if selector is None:
        return test_command
    if not SELECTOR_RE.fullmatch(selector):
        raise ToolError(f"invalid selector {selector!r}")
    return f"{test_command} {shlex.quote(selector)}"


def _run_tests(env: ToolEnv, args: dict[str, Any], test_command: str | None) -> dict[str, Any]:
    command = build_test_command(test_command, args.get("selector"))
    return env.execute({"command": command})


def run_tool(
    env: ToolEnv, action: dict[str, Any], *, test_command: str | None, output_limit: int
) -> dict[str, Any]:
    """Run a container tool other than bash; returns `{"output", "returncode",
    "exception_info"}` and raises `ToolError` for a refused or failed call.

    `output_limit` is applied by the caller (the agent keeps the full output on disk); it is
    accepted here so callers have one signature for every tool.
    """
    del output_limit
    tool = action.get("tool", "")
    args = validate_args(tool, action.get("args") or {})
    if tool == "read_file":
        return _read_file(env, args)
    if tool == "list_dir":
        return _list_dir(env, args)
    if tool == "search":
        return _search(env, args)
    if tool == "edit_file":
        return _edit_file(env, args)
    if tool == "write_file":
        return _write_file(env, args)
    if tool == "run_tests":
        return _run_tests(env, args, test_command)
    raise ToolError(f"{tool!r} is not a container tool")
