"""M8 T1: abuse-limit settings and sandbox runtime selection (contract §1.1, §1.3). No Docker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from swf.sandbox import SandboxSpec, parse_runtimes, runtime_from_env, sandbox_run_args
from swf.settings import AbuseLimits, SettingsError


def test_abuse_defaults_are_spec_values() -> None:
    a = AbuseLimits()
    assert (a.tasks_per_day, a.cost_per_task_usd, a.cost_per_day_usd, a.concurrent_tasks) == (
        5,
        1.0,
        3.0,
        1,
    )
    assert (a.api_per_minute, a.api_burst, a.create_per_minute, a.create_burst) == (
        120.0,
        60,
        6.0,
        3,
    )


def test_abuse_from_env_overrides_and_keeps_defaults() -> None:
    a = AbuseLimits.from_env(
        {
            "SWF_QUOTA_TASKS_PER_DAY": "10",
            "SWF_QUOTA_COST_PER_DAY_USD": "2.5",
            "SWF_RATE_API_BURST": "",
        }
    )
    assert (a.tasks_per_day, a.cost_per_day_usd, a.api_burst, a.concurrent_tasks) == (
        10,
        2.5,
        60,
        1,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWF_QUOTA_TASKS_PER_DAY", "x"),
        ("SWF_QUOTA_TASKS_PER_DAY", "0"),
        ("SWF_QUOTA_COST_PER_TASK_USD", "-1"),
        ("SWF_QUOTA_COST_PER_DAY_USD", "nan"),
        ("SWF_RATE_API_BURST", "1.5"),
        ("SWF_QUOTA_CONCURRENT_TASKS", "-2"),
    ],
)
def test_abuse_from_env_rejects_bad_values(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        AbuseLimits.from_env({name: value})


def test_abuse_direct_construction_validates() -> None:
    with pytest.raises(SettingsError):
        AbuseLimits(tasks_per_day=0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("  ", None), ("runsc", "runsc"), ("runc", "runc")],
)
def test_runtime_from_env(raw: str | None, expected: str | None) -> None:
    env = {} if raw is None else {"SWF_SANDBOX_RUNTIME": raw}
    assert runtime_from_env(env) == expected


def test_runtime_from_env_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="SWF_SANDBOX_RUNTIME"):
        runtime_from_env({"SWF_SANDBOX_RUNTIME": "kata"})


def test_spec_runtime_defaults_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fields: dict[str, Any] = {
        "task_id": "t1",
        "image": "python:3.12-slim",
        "workspace": tmp_path,
        "proxy_url": "http://10.0.0.2:3128",
        "network": "swf-task-t1",
    }
    monkeypatch.setenv("SWF_SANDBOX_RUNTIME", "runsc")
    args = sandbox_run_args(SandboxSpec(**fields))
    assert args[args.index("--runtime") + 1] == "runsc"
    monkeypatch.delenv("SWF_SANDBOX_RUNTIME")
    assert "--runtime" not in sandbox_run_args(SandboxSpec(**fields))
    with pytest.raises(ValidationError):
        SandboxSpec(**fields, runtime="bogus")


def test_parse_runtimes() -> None:
    assert parse_runtimes('{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}') == {
        "runc",
        "runsc",
    }
    assert parse_runtimes("{}") == frozenset()
