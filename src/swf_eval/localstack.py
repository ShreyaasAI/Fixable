"""The isolated local eval stack (m9-contract §0.9, §0.17): Postgres container, API, worker."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Any

import jwt

from swf_eval.seeds import REPO_ROOT

ISSUER = "http://eval.localhost/auth/v1"
SUPABASE_URL = "http://eval.localhost"
SHIM = REPO_ROOT / "tests/fixtures/supabase_shim.sql"
MIGRATIONS = REPO_ROOT / "supabase/migrations"
PG_IMAGE = "postgres:17-alpine"
_BASE_ALLOWED = ("PATH", "SYSTEMROOT", "PORTKEY_API_KEY")
# Not secrets: the Docker CLI and Python need these on Windows hosts.
_PLATFORM_ENV = ("TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "DOCKER_HOST")


def mint_jwt(secret: str, *, user_id: uuid.UUID, github_id: int, login: str, now: int) -> str:
    claims = {
        "iss": ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "sub": str(user_id),
        "iat": now,
        "exp": now + 12 * 3600,
        "app_metadata": {"provider": "github", "providers": ["github"]},
        "user_metadata": {"provider_id": str(github_id), "user_name": login},
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def child_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """Only PATH, SYSTEMROOT and PORTKEY_API_KEY from `base`, then `extra` (which wins)."""
    env = {k: base[k] for k in _BASE_ALLOWED if k in base}
    env.update(extra)
    return env


@dataclass(frozen=True)
class EvalUser:
    name: str
    user_id: uuid.UUID
    jwt: str


def _docker(*args: str) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def connect(db_url: str) -> Any:
    """A dict-row connection to the eval DB (backend role), e.g. for `records_from_db`."""
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(db_url, autocommit=True, row_factory=dict_row)


class LocalStack:
    """Context manager; `users`, `api_url` and `db_url` are set by `__enter__`."""

    def __init__(
        self,
        run_dir: Path,
        *,
        github_id: int,
        github_login: str,
        knobs: Mapping[str, str],
        user_names: tuple[str, ...] = ("eval-a", "eval-b"),
    ) -> None:
        self.run_dir = Path(run_dir)
        self.github_id = github_id
        self.github_login = github_login
        self.knobs = dict(knobs)
        self.user_names = user_names
        self.secret = secrets.token_urlsafe(48)
        self.users: dict[str, EvalUser] = {}
        self.api_url = ""
        self.db_url = ""
        self._cid: str | None = None
        self._procs: list[subprocess.Popen[bytes]] = []
        self._logs: list[IO[bytes]] = []

    def _start_db(self) -> None:
        import psycopg

        password = secrets.token_hex(16)
        self._cid = _docker(
            "run", "-d", "--rm", "--label", "swf.eval=1", "-e", f"POSTGRES_PASSWORD={password}",
            "-p", "127.0.0.1::5432", PG_IMAGE,
        )  # fmt: skip
        deadline = time.monotonic() + 90
        while True:
            try:
                port = _docker("port", self._cid, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
                url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
                with psycopg.connect(url, connect_timeout=2, autocommit=True) as conn:
                    conn.execute("select 1")
                break
            except (RuntimeError, IndexError, psycopg.OperationalError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(SHIM.read_text(encoding="utf-8"))
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(migration.read_text(encoding="utf-8"))
            now = int(time.time())
            meta = {"user_name": self.github_login, "provider_id": str(self.github_id)}
            for name in self.user_names:
                uid = uuid.uuid4()
                conn.execute(
                    "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
                    [uid, f"{name}@eval.localhost", json.dumps(meta)],
                )
                token = mint_jwt(
                    self.secret,
                    user_id=uid,
                    github_id=self.github_id,
                    login=self.github_login,
                    now=now,
                )
                self.users[name] = EvalUser(name, uid, token)
        self.db_url = url

    def env(self) -> dict[str, str]:
        """The API/worker env, built from scratch (§0.17)."""
        from cryptography.fernet import Fernet

        from swf.egress import EgressConfig

        extra = {k: os.environ[k] for k in _PLATFORM_ENV if os.environ.get(k)}
        extra.update(
            {
                "SWF_DATABASE_URL": self.db_url,
                "SWF_SUPABASE_URL": SUPABASE_URL,
                "SWF_SUPABASE_JWT_SECRET": self.secret,
                "SWF_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "SWF_WORKSPACES_ROOT": str(self.run_dir / "workspaces"),
                # the harness's own cache dir, so the worker computes the same conf-sha
                "SWF_PKGCACHE_DIR": str(EgressConfig().pkgcache_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
                # m10a §1.8: the worker requires it; a knob may set a real `pc-...` slug
                "SWF_PORTKEY_CONFIG": "off",
            }
        )
        extra.update(self.knobs)
        return child_env(os.environ, extra)

    def _spawn(self, name: str, argv: list[str], env: dict[str, str]) -> None:
        logs = self.run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = open(logs / f"{name}.log", "ab")  # noqa: SIM115 - closed in __exit__
        self._logs.append(log)
        self._procs.append(
            subprocess.Popen(argv, env=env, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)
        )

    def _wait_api(self, url: str) -> None:
        import httpx

        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{url}/healthz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline or any(p.poll() is not None for p in self._procs):
                raise RuntimeError("the eval API did not start (see logs/api.log)")
            time.sleep(0.5)

    def __enter__(self) -> LocalStack:
        try:
            self._start_db()
            env = self.env()
            port = _free_port()
            self._spawn(
                "api",
                [
                    sys.executable, "-m", "uvicorn", "swf.api.main:create_app_from_env",
                    "--factory", "--workers", "1", "--host", "127.0.0.1", "--port", str(port),
                ],
                env,
            )  # fmt: skip
            self.api_url = f"http://127.0.0.1:{port}"
            self._wait_api(self.api_url)
            self._spawn("worker", [sys.executable, "-m", "swf.worker"], env)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for proc in reversed(self._procs):
            proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._procs.clear()
        for log in self._logs:
            log.close()
        self._logs.clear()
        if self._cid:
            subprocess.run(["docker", "rm", "-f", self._cid], capture_output=True, check=False)
            self._cid = None
