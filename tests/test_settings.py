"""Environment-driven settings and the production app factory."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from swf.api.main import create_app_from_env
from swf.settings import Settings, SettingsError


def test_missing_required_settings_named() -> None:
    with pytest.raises(SettingsError, match="SWF_DATABASE_URL.*SWF_TOKEN_ENCRYPTION_KEY"):
        Settings.from_env({"SWF_SUPABASE_URL": "https://x.supabase.co"})


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "SWF_DATABASE_URL": "postgresql://u:p@h/db",
            "SWF_SUPABASE_URL": "https://ref.supabase.co/",
            "SWF_TOKEN_ENCRYPTION_KEY": "k",
            "SWF_SUPABASE_JWT_SECRET": "",
        }
    )
    assert s.jwt_issuer == "https://ref.supabase.co/auth/v1"
    assert s.supabase_jwt_secret is None
    assert s.github_api_url == "https://api.github.com"


def test_app_factory_builds_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWF_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("SWF_SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SWF_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = create_app_from_env()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/healthz", "/v1/me", "/v1/tasks", "/v1/repos"} <= paths
