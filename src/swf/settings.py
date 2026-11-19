"""Runtime settings from environment variables (no secrets in code or the repo)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    supabase_url: str  # https://<ref>.supabase.co
    token_encryption_key: str  # Fernet key
    supabase_jwt_secret: str | None = None  # only for legacy HS256 projects
    github_api_url: str = "https://api.github.com"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        required = ["SWF_DATABASE_URL", "SWF_SUPABASE_URL", "SWF_TOKEN_ENCRYPTION_KEY"]
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise SettingsError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            database_url=e["SWF_DATABASE_URL"],
            supabase_url=e["SWF_SUPABASE_URL"],
            token_encryption_key=e["SWF_TOKEN_ENCRYPTION_KEY"],
            supabase_jwt_secret=e.get("SWF_SUPABASE_JWT_SECRET") or None,
            github_api_url=e.get("SWF_GITHUB_API_URL") or "https://api.github.com",
        )
