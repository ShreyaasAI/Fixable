"""M9 T1: sandbox user, `index_url` pip env and the agent env knobs (contract 1.1, 1.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from swf.sandbox import SandboxSpec, SandboxUserError, sandbox_run_args, sandbox_user
from swf.settings import SettingsError
from swf.worker import agent_overrides_from_env


def _spec(tmp_path: Path, **overrides: object) -> SandboxSpec:
    fields: dict[str, object] = {
        "task_id": "t1",
        "image": "python:3.12-slim",
        "workspace": tmp_path,
        "user": "1000:1000",
        "proxy_url": "http://10.0.0.2:3128",
        "network": "swf-task-t1",
    }
    return SandboxSpec(**{**fields, **overrides})  # type: ignore[arg-type]


def test_windows_default_is_1000() -> None:
    assert sandbox_user({}, host_ids=None) == "1000:1000"


def test_posix_uses_host_ids() -> None:
    assert sandbox_user({}, host_ids=(1001, 127)) == "1001:127"


@pytest.mark.parametrize("ids", [(0, 0), (0, 1000), (1000, 0)])
def test_root_is_refused(ids: tuple[int, int]) -> None:
    with pytest.raises(SandboxUserError, match="SWF_SANDBOX_USER"):
        sandbox_user({}, host_ids=ids)


def test_env_override_wins_even_for_root_host() -> None:
    assert sandbox_user({"SWF_SANDBOX_USER": "1000:1000"}, host_ids=(0, 0)) == "1000:1000"


def test_blank_env_override_is_ignored() -> None:
    assert sandbox_user({"SWF_SANDBOX_USER": "  "}, host_ids=(1001, 127)) == "1001:127"


@pytest.mark.parametrize("raw", ["0:0", "1000", "1000:0", "a:b", "-1:5", "1000:1000:1"])
def test_env_override_validated(raw: str) -> None:
    with pytest.raises(ValueError, match="SWF_SANDBOX_USER"):
        sandbox_user({"SWF_SANDBOX_USER": raw}, host_ids=None)


def test_spec_default_user_follows_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SWF_SANDBOX_USER", "2001:2002")
    spec = SandboxSpec(
        task_id="t1",
        image="python:3.12-slim",
        workspace=tmp_path,
        proxy_url="http://10.0.0.2:3128",
        network="n",
    )
    assert spec.user == "2001:2002"


def test_index_url_sets_pip_env(tmp_path: Path) -> None:
    args = sandbox_run_args(_spec(tmp_path, index_url="http://10.0.0.3:3141/simple/"))
    for pair in (
        "PIP_INDEX_URL=http://10.0.0.3:3141/simple/",
        "PIP_TRUSTED_HOST=10.0.0.3",
        "NO_PROXY=10.0.0.3",
        "no_proxy=10.0.0.3",
        "HTTPS_PROXY=http://10.0.0.2:3128",
    ):
        assert pair in args


def test_no_index_url_leaves_pip_env_unset(tmp_path: Path) -> None:
    args = sandbox_run_args(_spec(tmp_path))
    assert not any(a.startswith(("PIP_INDEX_URL=", "PIP_TRUSTED_HOST=")) for a in args)
    assert "NO_PROXY=" in args
    assert "no_proxy=" in args


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/simple/",
        "https://10.0.0.3:3141/simple/",
        "http://10.0.0.3/simple/",
        "http://10.0.0.3:3141/other/",
    ],
)
def test_index_url_validated(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, index_url=url)


def test_agent_overrides() -> None:
    assert agent_overrides_from_env({}) == {}
    assert (
        agent_overrides_from_env({"SWF_AGENT_STEP_LIMIT": " ", "SWF_AGENT_WALL_TIME_SECONDS": ""})
        == {}
    )
    assert agent_overrides_from_env(
        {"SWF_AGENT_STEP_LIMIT": "80", "SWF_AGENT_WALL_TIME_SECONDS": "1800"}
    ) == {"step_limit": 80, "wall_time_limit_seconds": 1800}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWF_AGENT_STEP_LIMIT", "0"),
        ("SWF_AGENT_STEP_LIMIT", "x"),
        ("SWF_AGENT_WALL_TIME_SECONDS", "-5"),
        ("SWF_AGENT_WALL_TIME_SECONDS", "1.5"),
    ],
)
def test_agent_overrides_rejects(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        agent_overrides_from_env({name: value})
