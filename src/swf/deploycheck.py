"""Deploy readiness check (m10b §0.7).

    uv run --env-file .env python -m swf.deploycheck

Prints one PASS/FAIL line per check, with the fix for each failure, and exits 1 if any fails.
It never prints a value from the environment.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

REQUIRED_ENV = (
    "SWF_DATABASE_URL",
    "SWF_SUPABASE_URL",
    "SWF_TOKEN_ENCRYPTION_KEY",
    "PORTKEY_API_KEY",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix: str = ""


def _env(e: Mapping[str, str]) -> Check:
    missing = [k for k in REQUIRED_ENV if not (e.get(k) or "").strip()]
    return Check("env", not missing, f"set {', '.join(missing)} in .env" if missing else "")


def _token_key(e: Mapping[str, str]) -> Check:
    from cryptography.fernet import Fernet

    try:
        Fernet((e.get("SWF_TOKEN_ENCRYPTION_KEY") or "").strip())
    except (ValueError, TypeError):
        return Check(
            "token key",
            False,
            'SWF_TOKEN_ENCRYPTION_KEY must be a Fernet key: uv run python -c "from '
            'cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
        )
    return Check("token key", True)


def _portkey(e: Mapping[str, str]) -> Check:
    from swf.agent.model import portkey_config_from_env
    from swf.settings import SettingsError

    fix = "set SWF_PORTKEY_CONFIG to the saved guardrail config slug (pc-...), not off"
    try:
        return Check("portkey guardrails", portkey_config_from_env(e) is not None, fix)
    except SettingsError:
        return Check("portkey guardrails", False, fix)


def _public(e: Mapping[str, str]) -> Check:
    flag = (e.get("SWF_PUBLIC") or "").strip().lower()
    return Check("public mode", flag in {"1", "true"}, "set SWF_PUBLIC=1 for a public deployment")


def _cors(e: Mapping[str, str]) -> Check:
    from swf.settings import Deployment, SettingsError

    fix = "set SWF_CORS_ORIGINS to the Vercel origin, e.g. https://<project>.vercel.app"
    try:
        origins = Deployment.from_env({"SWF_CORS_ORIGINS": e.get("SWF_CORS_ORIGINS", "")})
    except SettingsError as exc:
        return Check("cors origins", False, f"{exc}; {fix}")
    ok = any(o.startswith("https://") for o in origins.cors_origins)
    return Check("cors origins", ok, fix)


def _gvisor(e: Mapping[str, str], runtimes: frozenset[str]) -> Check:
    configured = (e.get("SWF_SANDBOX_RUNTIME") or "").strip() == "runsc"
    fix = (
        "install gVisor where Docker runs (m10-runbook section 6) and set SWF_SANDBOX_RUNTIME=runsc"
        if "runsc" not in runtimes
        else "set SWF_SANDBOX_RUNTIME=runsc"
    )
    return Check("gvisor", configured and "runsc" in runtimes, fix)


def _sandbox_user(e: Mapping[str, str]) -> Check:
    from swf.sandbox import SandboxUserError, sandbox_user

    try:
        sandbox_user(e)
    except (SandboxUserError, ValueError):
        return Check("sandbox user", False, "set SWF_SANDBOX_USER to a non-root uid:gid")
    return Check("sandbox user", True)


def checks(env: Mapping[str, str], *, docker_runtimes: frozenset[str]) -> list[Check]:
    steps: list[Callable[[], Check]] = [
        lambda: _env(env),
        lambda: _token_key(env),
        lambda: _portkey(env),
        lambda: _public(env),
        lambda: _cors(env),
        lambda: _gvisor(env, docker_runtimes),
        lambda: _sandbox_user(env),
    ]
    return [step() for step in steps]


def report(results: list[Check]) -> int:
    for c in results:
        line = f"PASS {c.name}" if c.ok else f"FAIL {c.name}: {c.fix}"
        print(line)
    failed = [c for c in results if not c.ok]
    print("ready to deploy" if not failed else f"{len(failed)} check(s) failed")
    return 1 if failed else 0


def main() -> int:
    from swf.sandbox import docker_runtimes

    return report(checks(os.environ, docker_runtimes=docker_runtimes()))


if __name__ == "__main__":
    sys.exit(main())
