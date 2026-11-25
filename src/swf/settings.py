"""Runtime settings from environment variables (no secrets in code or the repo)."""

from __future__ import annotations

import math
import os
import re
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
    ("preflight_per_minute", "SWF_RATE_PREFLIGHT_PER_MINUTE", float),
    ("preflight_burst", "SWF_RATE_PREFLIGHT_BURST", int),
    ("sse_streams_per_user", "SWF_SSE_STREAMS_PER_USER", int),
    ("sse_streams_total", "SWF_SSE_STREAMS_TOTAL", int),
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
    preflight_per_minute: float = 10.0  # m10b §0.5
    preflight_burst: int = 5
    sse_streams_per_user: int = 4  # m10b §0.6: each open stream holds a threadpool thread
    sse_streams_total: int = 40

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


# --- deployment (m10b §0.2-0.3) ---------------------------------------------------------------

_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_ORIGIN_RE = re.compile(r"^(https?)://([a-z0-9.-]+)(?::([0-9]{1,5}))?$")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRUE = {"1", "true"}
_FALSE = {"", "0", "false"}


def _origin(raw: str) -> str:
    """One exact browser origin, normalised as browsers send it (lowercase, no default port):
    https, or http on localhost; no path, query, userinfo or wildcard."""
    value = raw.strip().removesuffix("/").lower()
    m = _ORIGIN_RE.fullmatch(value)
    port = int(m.group(3)) if m and m.group(3) else None
    if (
        m is None
        or (m.group(1) == "http" and m.group(2) not in _LOCAL_HOSTS)
        or (port is not None and not 1 <= port <= 65535)
        or m.group(2).startswith(".")
        or m.group(2).endswith(".")
    ):
        raise SettingsError(
            "SWF_CORS_ORIGINS: each entry must be an origin like https://app.example.com "
            "(https only, except http://localhost or http://127.0.0.1)"
        )
    scheme, host = m.group(1), m.group(2)
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _is_local(origin: str) -> bool:
    host = origin.split("://", 1)[1].split(":", 1)[0]
    return host in _LOCAL_HOSTS


@dataclass(frozen=True)
class Deployment:
    """Public mode and the CORS allowlist (`SWF_PUBLIC`, `SWF_CORS_ORIGINS`)."""

    public: bool = False
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Deployment:
        e = os.environ if env is None else env
        flag = (e.get("SWF_PUBLIC") or "").strip().lower()
        if flag not in _TRUE | _FALSE:
            raise SettingsError("SWF_PUBLIC must be 1 or 0")
        raw = (e.get("SWF_CORS_ORIGINS") or "").strip()
        origins = tuple(_origin(o) for o in raw.split(",")) if raw else ()
        public = flag in _TRUE
        if public and not origins:
            raise SettingsError("SWF_PUBLIC=1 needs SWF_CORS_ORIGINS (the web app's origin)")
        if not public and any(not _is_local(o) for o in origins):
            # Serving a public origin is exposure: the worker's gVisor gate must be on too.
            raise SettingsError("a non-localhost SWF_CORS_ORIGINS entry needs SWF_PUBLIC=1")
        return cls(public=public, cors_origins=origins)
