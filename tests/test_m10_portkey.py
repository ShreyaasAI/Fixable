"""M10a T6: Portkey guardrails on by default (m10a-contract §1.8).

`portkey_config_from_env`, `PhaseModel` reading the config through it, and the worker's
fail-fast `portkey_startup_check` that `main()` calls before any other startup step.
Decision: after `strip()` the value must be exactly `off` (lowercase) or a `pc-...` slug;
`OFF`, blank, `pc-` and anything else is a `SettingsError` naming `SWF_PORTKEY_CONFIG`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import FakeCompletion, tool_response

import swf.sandbox
import swf.settings
import swf.worker
from swf.agent.model import PhaseModel, portkey_config_from_env  # type: ignore[attr-defined]
from swf.settings import SettingsError

KEY = "pk-test-secret-0123456789"
SLUG = "pc-abc-123"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "task"}]


# --- portkey_config_from_env -------------------------------------------------------------------


def test_off_disables_the_config() -> None:
    assert portkey_config_from_env({"SWF_PORTKEY_CONFIG": "off"}) is None


def test_a_valid_slug_is_returned() -> None:
    assert portkey_config_from_env({"SWF_PORTKEY_CONFIG": SLUG}) == SLUG


def test_surrounding_whitespace_is_stripped() -> None:
    assert portkey_config_from_env({"SWF_PORTKEY_CONFIG": f"  {SLUG}\n"}) == SLUG
    assert portkey_config_from_env({"SWF_PORTKEY_CONFIG": " off "}) is None


@pytest.mark.parametrize(
    "value", [None, "", "   ", "pc-", "bad", "OFF", "Off", "off now", "pc-a_b"]
)
def test_missing_blank_or_malformed_raises_settings_error(value: str | None) -> None:
    env = {} if value is None else {"SWF_PORTKEY_CONFIG": value}
    with pytest.raises(SettingsError, match="SWF_PORTKEY_CONFIG"):
        portkey_config_from_env(env)


# --- PhaseModel --------------------------------------------------------------------------------


def make(env: dict[str, str]) -> tuple[PhaseModel, FakeCompletion]:
    fake = FakeCompletion(tool_response("bash", {"command": "ls"}))
    return PhaseModel.from_env(env, completion=fake), fake


def test_phase_model_with_off_sends_no_config_header() -> None:
    model, fake = make({"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "off"})
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["extra_headers"] == {"x-portkey-api-key": KEY}


def test_phase_model_with_a_slug_sends_the_config_header() -> None:
    model, fake = make({"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": SLUG})
    model.query(MESSAGES)
    (sent,) = fake.calls
    assert sent["extra_headers"] == {"x-portkey-api-key": KEY, "x-portkey-config": SLUG}


def test_phase_model_from_env_without_the_variable_raises() -> None:
    with pytest.raises(SettingsError, match="SWF_PORTKEY_CONFIG"):
        PhaseModel.from_env({"PORTKEY_API_KEY": KEY}, completion=FakeCompletion())


def test_phase_model_from_env_with_a_bad_value_raises() -> None:
    with pytest.raises(SettingsError, match="SWF_PORTKEY_CONFIG"):
        PhaseModel.from_env(
            {"PORTKEY_API_KEY": KEY, "SWF_PORTKEY_CONFIG": "nope"}, completion=FakeCompletion()
        )


# --- worker startup ----------------------------------------------------------------------------


def test_startup_check_returns_the_slug_silently(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="swf.worker"):
        assert swf.worker.portkey_startup_check({"SWF_PORTKEY_CONFIG": SLUG}) == SLUG
    assert not caplog.records


def test_startup_check_on_off_warns_that_guardrails_are_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="swf.worker"):
        assert swf.worker.portkey_startup_check({"SWF_PORTKEY_CONFIG": "off"}) is None
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "portkey" in text.lower()
    assert "guardrails" in text.lower()
    assert "off" in text.lower()


def test_startup_check_without_the_variable_raises() -> None:
    with pytest.raises(SettingsError, match="SWF_PORTKEY_CONFIG"):
        swf.worker.portkey_startup_check({})


def test_main_without_the_config_fails_before_any_other_startup_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def record(name: str) -> Any:
        def fn(*_a: Any, **_k: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not run before the Portkey check")

        return fn

    class RecordingDatabase:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            calls.append("Database()")
            raise AssertionError("Database must not be constructed before the Portkey check")

    monkeypatch.delenv("SWF_PORTKEY_CONFIG", raising=False)
    monkeypatch.setenv("SWF_MSWEA_CONFIG_DIR", str(tmp_path / "mswea"))
    monkeypatch.setattr(swf.worker, "Database", RecordingDatabase)
    monkeypatch.setattr(swf.sandbox, "sandbox_user", record("sandbox_user"))
    monkeypatch.setattr(swf.sandbox, "check_runtime", record("check_runtime"))
    monkeypatch.setattr(swf.sandbox, "runtime_from_env", record("runtime_from_env"))
    monkeypatch.setattr(
        swf.settings.Settings, "from_env", classmethod(lambda *_a, **_k: record("Settings")())
    )
    with pytest.raises((SettingsError, SystemExit)):
        swf.worker.main()
    assert calls == []
