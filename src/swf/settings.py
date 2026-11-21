"""Runtime settings from environment variables (no secrets in code or the repo)."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


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


_ABUSE_ENV: tuple[tuple[str, str, type[int] | type[float]], ...] = (
    ("tasks_per_day", "SWF_QUOTA_TASKS_PER_DAY", int),
    ("cost_per_task_usd", "SWF_QUOTA_COST_PER_TASK_USD", float),
    ("cost_per_day_usd", "SWF_QUOTA_COST_PER_DAY_USD", float),
    ("concurrent_tasks", "SWF_QUOTA_CONCURRENT_TASKS", int),
    ("api_per_minute", "SWF_RATE_API_PER_MINUTE", float),
    ("api_burst", "SWF_RATE_API_BURST", int),
    ("create_per_minute", "SWF_RATE_CREATE_PER_MINUTE", float),
    ("create_burst", "SWF_RATE_CREATE_BURST", int),
)


@dataclass(frozen=True)
class AbuseLimits:
    """Quota and rate-limit values (spec §12 defaults); every value is > 0."""

    tasks_per_day: int = 5
    cost_per_task_usd: float = 1.0
    cost_per_day_usd: float = 3.0
    concurrent_tasks: int = 1
    api_per_minute: float = 120.0
    api_burst: int = 60
    create_per_minute: float = 6.0
    create_burst: int = 3

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if not (math.isfinite(v) and v > 0):
                raise SettingsError(f"{f.name} must be > 0, got {v!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AbuseLimits:
        e = os.environ if env is None else env
        values: dict[str, int | float] = {}
        for field_name, var, kind in _ABUSE_ENV:
            raw = (e.get(var) or "").strip()
            if not raw:
                continue
            try:
                v = kind(raw)
            except ValueError:
                raise SettingsError(f"{var} must be a {kind.__name__}, got {raw!r}") from None
            if not (math.isfinite(v) and v > 0):
                raise SettingsError(f"{var} must be a finite number > 0, got {raw!r}")
            values[field_name] = v
        return cls(**values)  # type: ignore[arg-type]
