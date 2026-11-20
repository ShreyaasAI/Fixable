"""`swf._mswea` import-order guard and the upstream subclass contract (m4-contract §1, header)."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import minisweagent
import pytest
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow

from swf import _mswea

ENV_VARS = (
    "MSWEA_GLOBAL_CONFIG_DIR",
    "MSWEA_SILENT_STARTUP",
    "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT",
)


@pytest.fixture
def restore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`isolate` overwrites os.environ; make monkeypatch restore the originals afterwards."""
    for name in ENV_VARS:
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.setenv(name, "placeholder")
            monkeypatch.delenv(name)


def test_isolate_sets_the_three_variables(restore_env: None) -> None:
    # minisweagent is already imported by `import swf`, so only its own directory is accepted.
    directory = Path(minisweagent.global_config_dir)
    os.environ["MSWEA_SILENT_STARTUP"] = "0"
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "10"
    result = _mswea.isolate(directory)
    assert Path(result) == directory
    assert Path(os.environ["MSWEA_GLOBAL_CONFIG_DIR"]) == directory
    assert os.environ["MSWEA_SILENT_STARTUP"] == "1"
    assert os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] == "1"


def test_isolate_is_idempotent(restore_env: None) -> None:
    directory = Path(minisweagent.global_config_dir)
    assert _mswea.isolate(directory) == _mswea.isolate(directory)


def test_isolate_refuses_a_directory_with_a_dotenv(tmp_path: Path, restore_env: None) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=leak\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _mswea.isolate(tmp_path)


def test_isolate_refuses_after_minisweagent_loaded_from_another_directory(
    tmp_path: Path, restore_env: None
) -> None:
    other = tmp_path / "other"
    assert Path(minisweagent.global_config_dir) != other
    with pytest.raises(RuntimeError):
        _mswea.isolate(other)


def test_default_directory_is_under_the_temp_dir() -> None:
    assert _mswea.ISOLATED_DIR_DEFAULT.name == "swf-mswea-empty"


def _clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MSWEA_", "SWF_MSWEA_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SWF_MSWEA_CONFIG_DIR"] = str(tmp_path / "isolated")
    env.update(extra)
    return env


def test_importing_swf_sandbox_ignores_a_planted_global_config(tmp_path: Path) -> None:
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / ".env").write_text("SWF_PLANTED_SECRET=leaked\n", encoding="utf-8")
    code = (
        "import os\n"
        "import swf.sandbox\n"
        "import minisweagent\n"
        "print('PLANTED=' + str(os.environ.get('SWF_PLANTED_SECRET')))\n"
        "print('DIR=' + str(minisweagent.global_config_dir))\n"
        "print('RETRY=' + str(os.environ.get('MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT')))\n"
    )
    env = _clean_env(tmp_path, MSWEA_GLOBAL_CONFIG_DIR=str(planted))
    run = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert run.returncode == 0, run.stderr
    assert "PLANTED=None" in run.stdout
    assert f"DIR={tmp_path / 'isolated'}" in run.stdout
    assert "RETRY=1" in run.stdout
    assert "mini-swe-agent" not in run.stdout + run.stderr
    assert "Loading global config" not in run.stdout + run.stderr


def test_importing_swf_fails_when_the_isolated_directory_holds_a_dotenv(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / ".env").write_text("SWF_PLANTED_SECRET=leaked\n", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-c", "import swf.sandbox"],
        capture_output=True,
        text=True,
        env=_clean_env(tmp_path),
        timeout=120,
    )
    assert run.returncode != 0
    assert "RuntimeError" in run.stderr


def test_importing_swf_sandbox_does_not_load_a_dotenv_above_the_cwd(tmp_path: Path) -> None:
    """litellm's import-time `load_dotenv()` walks up to the first `.env` (from the cwd under
    `python -c`; from litellm's own directory otherwise, which reached the main checkout's
    `.env`). `isolate()` sets `LITELLM_MODE=PRODUCTION`, which skips it."""
    (tmp_path / ".env").write_text("SWF_PLANTED_DOTENV=leaked\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    env = _clean_env(tmp_path)
    env.pop("LITELLM_MODE", None)
    env.pop("SWF_PLANTED_DOTENV", None)
    probe = "import os\nprint('PLANTED=' + str(os.environ.get('SWF_PLANTED_DOTENV')))\n"

    def run(imports: str) -> str:
        done = subprocess.run(
            [sys.executable, "-c", imports + probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=child,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    # Control: litellm on its own does load the planted file, so the probe can see a leak.
    assert "PLANTED=leaked" in run("import litellm\n")
    out = run("import os, swf.sandbox, swf.agent\nprint('MODE=' + os.environ['LITELLM_MODE'])\n")
    assert "PLANTED=None" in out
    assert "MODE=PRODUCTION" in out


# --- upstream contract the agent subclasses (mini-swe-agent 2.4.6) -------------------------


def test_upstream_version_is_pinned() -> None:
    assert minisweagent.__version__ == "2.4.6"


def _params(fn: object) -> list[tuple[str, inspect._ParameterKind]]:
    return [(n, p.kind) for n, p in inspect.signature(fn).parameters.items()]  # type: ignore[arg-type]


def test_default_agent_hook_signatures() -> None:
    P = inspect.Parameter
    assert _params(DefaultAgent.execute_actions) == [
        ("self", P.POSITIONAL_OR_KEYWORD),
        ("message", P.POSITIONAL_OR_KEYWORD),
    ]
    assert _params(DefaultAgent.add_messages) == [
        ("self", P.POSITIONAL_OR_KEYWORD),
        ("messages", P.VAR_POSITIONAL),
    ]
    assert _params(DefaultAgent.query) == [("self", P.POSITIONAL_OR_KEYWORD)]
    assert _params(DefaultAgent.step) == [("self", P.POSITIONAL_OR_KEYWORD)]
    assert _params(DefaultAgent.run) == [
        ("self", P.POSITIONAL_OR_KEYWORD),
        ("task", P.POSITIONAL_OR_KEYWORD),
        ("kwargs", P.VAR_KEYWORD),
    ]


def test_interrupt_agent_flow_carries_messages() -> None:
    exc = InterruptAgentFlow({"a": 1}, {"b": 2})
    assert exc.messages == ({"a": 1}, {"b": 2})
