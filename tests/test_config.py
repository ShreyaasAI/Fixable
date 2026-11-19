"""Tests for the AGENTS.md loader (implementation plan M0, spec §4 item 4)."""

from pathlib import Path

import pytest

from swf.config import (
    AgentConfig,
    InvalidConfigError,
    MissingConfigBlockError,
    MultipleConfigBlocksError,
    load_agents_file,
    parse_agents_md,
)

VALID = """# Project guide

Use pytest. Keep functions pure.

```yaml agent-config
image: python:3.12-slim
setup:
  - pip install -e .
test: python -m pytest -q
allow:
  - "python -m pytest*"
resources:
  memory_mb: 1024
  cpus: 1
```

## Style
Prefer small diffs.
"""


def test_parses_config_block() -> None:
    loaded = parse_agents_md(VALID)
    assert loaded.config == AgentConfig(
        image="python:3.12-slim",
        setup=["pip install -e ."],
        test="python -m pytest -q",
        allow=["python -m pytest*"],
        resources={"memory_mb": 1024, "cpus": 1},  # type: ignore[arg-type]
    )


def test_prose_excludes_config_block() -> None:
    loaded = parse_agents_md(VALID)
    assert "Use pytest. Keep functions pure." in loaded.prose
    assert "Prefer small diffs." in loaded.prose
    assert "agent-config" not in loaded.prose
    assert "image:" not in loaded.prose


def test_defaults_for_optional_fields() -> None:
    text = "```yaml agent-config\nimage: alpine\ntest: make test\n```\n"
    cfg = parse_agents_md(text).config
    assert cfg.setup == []
    assert cfg.allow == []
    assert cfg.resources.memory_mb == 2048
    assert cfg.resources.cpus == 1.0


def test_crlf_line_endings() -> None:
    text = VALID.replace("\n", "\r\n")
    assert parse_agents_md(text).config.image == "python:3.12-slim"


def test_ordinary_yaml_block_is_prose_not_config() -> None:
    text = "```yaml\nfoo: bar\n```\n"
    with pytest.raises(MissingConfigBlockError):
        parse_agents_md(text)


def test_missing_block() -> None:
    with pytest.raises(MissingConfigBlockError):
        parse_agents_md("# Just prose\n")


def test_multiple_blocks() -> None:
    block = "```yaml agent-config\nimage: a\ntest: t\n```\n"
    with pytest.raises(MultipleConfigBlocksError):
        parse_agents_md(block + block)


def test_unterminated_block() -> None:
    with pytest.raises(InvalidConfigError, match="not closed"):
        parse_agents_md("```yaml agent-config\nimage: a\ntest: t\n")


def test_invalid_yaml() -> None:
    with pytest.raises(InvalidConfigError, match="YAML"):
        parse_agents_md("```yaml agent-config\nimage: [unclosed\n```\n")


def test_non_mapping_yaml() -> None:
    with pytest.raises(InvalidConfigError, match="mapping"):
        parse_agents_md("```yaml agent-config\n- a\n- b\n```\n")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("test: t\n", "image"),
        ("image: a\n", "test"),
        ("image: a\ntest: t\nunknown: 1\n", "unknown"),
        ("image: a\ntest: t\nresources:\n  memory_mb: 0\n", "memory_mb"),
        ("image: a\ntest: t\nresources:\n  cpus: -1\n", "cpus"),
        ("image: ''\ntest: t\n", "image"),
    ],
)
def test_schema_errors_name_the_field(body: str, field: str) -> None:
    with pytest.raises(InvalidConfigError, match=field):
        parse_agents_md(f"```yaml agent-config\n{body}```\n")


def test_load_from_repo_dir(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(VALID, encoding="utf-8")
    assert load_agents_file(tmp_path).config.test == "python -m pytest -q"


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MissingConfigBlockError, match=r"AGENTS\.md"):
        load_agents_file(tmp_path)
