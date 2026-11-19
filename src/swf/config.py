"""AGENTS.md loader.

`AGENTS.md` serves two audiences (spec §4 item 4):
- prose: guidance injected into the agent prompt;
- one fenced block with info string ``yaml agent-config``: machine config parsed by the backend.

Resource values are not capped here; pre-flight (M2) compares them with the per-task threshold
and warns the user before the task starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

AGENTS_FILENAME = "AGENTS.md"
CONFIG_INFO_STRING = "yaml agent-config"

_FENCE_OPEN = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*?)[ \t]*$")


class AgentsFileError(Exception):
    """Base error for AGENTS.md problems; messages are shown to the user."""


class MissingConfigBlockError(AgentsFileError):
    pass


class MultipleConfigBlocksError(AgentsFileError):
    pass


class InvalidConfigError(AgentsFileError):
    pass


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_mb: int = Field(default=2048, gt=0)
    cpus: float = Field(default=1.0, gt=0)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(min_length=1)
    setup: list[str] = Field(default_factory=list)
    test: str = Field(min_length=1)
    allow: list[str] = Field(default_factory=list)
    resources: Resources = Field(default_factory=Resources)


@dataclass(frozen=True)
class AgentsFile:
    prose: str
    config: AgentConfig


def _split_blocks(text: str) -> tuple[str, list[str]]:
    """Return (prose, config_block_bodies). Other fenced blocks stay in the prose."""
    lines = text.splitlines()
    prose: list[str] = []
    configs: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            prose.append(lines[i])
            i += 1
            continue
        fence = m.group("fence")
        is_config = m.group("info") == CONFIG_INFO_STRING
        close = re.compile(rf"^{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        if j == len(lines):
            if is_config:
                raise InvalidConfigError("The `yaml agent-config` block is not closed.")
            prose.extend(lines[i:])
            break
        if is_config:
            configs.append("\n".join(lines[i + 1 : j]))
        else:
            prose.extend(lines[i : j + 1])
        i = j + 1
    return "\n".join(prose).strip(), configs


def _format_validation_error(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "<root>"
        parts.append(f"{loc}: {e['msg']}")
    return "Invalid agent-config: " + "; ".join(parts)


def parse_agents_md(text: str) -> AgentsFile:
    prose, blocks = _split_blocks(text)
    if not blocks:
        raise MissingConfigBlockError(
            f"{AGENTS_FILENAME} has no ```{CONFIG_INFO_STRING} block declaring image and test."
        )
    if len(blocks) > 1:
        raise MultipleConfigBlocksError(
            f"{AGENTS_FILENAME} has {len(blocks)} `{CONFIG_INFO_STRING}` blocks; expected one."
        )
    try:
        data = yaml.safe_load(blocks[0])
    except yaml.YAMLError as e:
        raise InvalidConfigError(f"agent-config is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise InvalidConfigError("agent-config must be a YAML mapping.")
    try:
        config = AgentConfig.model_validate(data)
    except ValidationError as e:
        raise InvalidConfigError(_format_validation_error(e)) from e
    return AgentsFile(prose=prose, config=config)


def load_agents_file(repo_dir: Path) -> AgentsFile:
    path = repo_dir / AGENTS_FILENAME
    if not path.is_file():
        raise MissingConfigBlockError(f"{AGENTS_FILENAME} not found in repository root.")
    return parse_agents_md(path.read_text(encoding="utf-8"))
