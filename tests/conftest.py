"""Shared fixtures.

DB tests run against a throwaway `postgres:17-alpine` container (or `SWF_TEST_DATABASE_URL`,
which must point at an empty database on a server where this role may create databases). The
Supabase shim and every file in `supabase/migrations/` are applied once into a `swf_template`
database; the session `db` is a clone of it and `fresh_db` is a per-test clone (dropped after the
test), so capacity-sensitive tests are not affected by other tests' rows. Without Docker the DB
tests are skipped and the skip is reported (`-rs` in addopts).

Tests marked `docker` are skipped (with a reason) when `docker info` fails; tests marked
`network` are skipped when `SWF_SKIP_NETWORK=1`.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from helpers import ISSUER, FakeGitHub, FakeRegistry, FakeUser, index_manifest, mint
from psycopg import sql
from psycopg.conninfo import make_conninfo

from swf.api.app import AppDeps, create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.egress import EgressConfig, EgressInfo, ensure_egress, teardown_egress
from swf.github import GitHubClient
from swf.images import RegistryClient
from swf.preflight import PreflightService
from swf.settings import AbuseLimits
from swf.tokens import TokenCipher

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "tests" / "fixtures" / "supabase_shim.sql"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
PG_IMAGE = "postgres:17-alpine"
# One id per test run: Docker names and the template database carry it, so parallel runs (other
# worktrees, the checker) never touch each other's containers, networks or databases.
RUN_ID = uuid.uuid4().hex[:8]
TEMPLATE_DB = f"swf_template_{RUN_ID}"


def _start_postgres() -> tuple[str, str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker unavailable: docker CLI not found (DB tests skipped)")
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--label", "swf.test=1",
            "-e", "POSTGRES_PASSWORD=postgres", "-p", "127.0.0.1::5432", PG_IMAGE,
        ],
        capture_output=True, text=True,
    )  # fmt: skip
    if run.returncode != 0:
        pytest.skip(f"Docker unavailable: {run.stderr.strip()} (DB tests skipped)")
    cid = run.stdout.strip()
    deadline = time.monotonic() + 60
    # The host port is published shortly after `docker run` returns; poll for it.
    port = ""
    while not port:
        out = subprocess.run(["docker", "port", cid, "5432/tcp"], capture_output=True, text=True)
        lines = out.stdout.strip().splitlines()
        port = lines[0].rsplit(":", 1)[1] if lines else ""
        if not port:
            if time.monotonic() > deadline:
                subprocess.run(["docker", "stop", cid], capture_output=True)
                pytest.fail(f"postgres container {cid[:12]} never published port 5432")
            time.sleep(0.2)
    url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("select 1")
            return cid, url
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                subprocess.run(["docker", "stop", cid], capture_output=True)
                raise
            time.sleep(0.5)


def _clone_database(admin_url: str, name: str) -> str:
    """CREATE DATABASE <name> TEMPLATE swf_template; returns the URL of the clone."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(TEMPLATE_DB)
            )
        )
    return make_conninfo(admin_url, dbname=name)


def _drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="session")
def pg_admin_url() -> Iterator[str]:
    """Server URL (maintenance database); `swf_template` holds the shim + all migrations."""
    external = os.environ.get("SWF_TEST_DATABASE_URL")
    cid: str | None = None
    if external:
        url = external
    else:
        cid, url = _start_postgres()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("create database {}").format(sql.Identifier(TEMPLATE_DB)))
        # This connection is closed before any clone: CREATE DATABASE ... TEMPLATE needs the
        # template to have no open connections.
        with psycopg.connect(make_conninfo(url, dbname=TEMPLATE_DB), autocommit=True) as conn:
            conn.execute(SHIM.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            for migration in MIGRATIONS:
                conn.execute(migration.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        yield url
    finally:
        if cid:
            subprocess.run(["docker", "stop", cid], capture_output=True)
        else:
            _drop_database(url, TEMPLATE_DB)


@pytest.fixture(scope="session")
def postgres_url(pg_admin_url: str) -> Iterator[str]:
    """URL of the session database (a clone of the migrated template)."""
    name = f"swf_session_{uuid.uuid4().hex[:8]}"
    url = _clone_database(pg_admin_url, name)
    yield url
    _drop_database(pg_admin_url, name)


@pytest.fixture(scope="session")
def db(postgres_url: str) -> Iterator[Database]:
    database = Database(postgres_url)
    database.open()
    yield database
    database.close()


@pytest.fixture
def fresh_db(pg_admin_url: str) -> Iterator[Database]:
    """A private clone of the migrated template, dropped after the test."""
    name = f"swf_test_{uuid.uuid4().hex[:8]}"
    database = Database(_clone_database(pg_admin_url, name))
    database.open()
    try:
        yield database
    finally:
        database.close()
        _drop_database(pg_admin_url, name)


@pytest.fixture
def make_user(db: Database) -> Callable[..., FakeUser]:
    def _make(login: str | None = None) -> FakeUser:
        uid = uuid.uuid4()
        user = FakeUser(
            id=uid, github_id=random.randint(1, 2**40), login=login or f"user-{uid.hex[:8]}"
        )
        meta = {"user_name": user.login, "provider_id": str(user.github_id)}
        with db.backend() as conn:
            conn.execute(
                "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
                [user.id, f"{user.login}@example.test", json.dumps(meta)],
            )
        return user

    return _make


@pytest.fixture(scope="session")
def signing_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def verifier(signing_key: ec.EllipticCurvePrivateKey) -> JwtVerifier:
    public = signing_key.public_key()
    return JwtVerifier(issuer=ISSUER, key_resolver=lambda _token: public)


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode())


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry() -> FakeRegistry:
    """python:3.12-slim as a multi-arch image on Docker Hub."""
    reg = FakeRegistry()
    reg.manifests[("registry-1.docker.io", "library/python", "3.12-slim")] = index_manifest(
        "linux/amd64", "linux/arm64/v8"
    )
    return reg


# Every AbuseLimits field at 1_000_000: M1-M7 API tests never hit a quota or rate limit (m8 s2).
PERMISSIVE = AbuseLimits(
    tasks_per_day=1_000_000,
    cost_per_task_usd=1_000_000.0,
    cost_per_day_usd=1_000_000.0,
    concurrent_tasks=1_000_000,
    api_per_minute=1_000_000.0,
    api_burst=1_000_000,
    create_per_minute=1_000_000.0,
    create_burst=1_000_000,
)


def build_deps(
    db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
    *,
    abuse: AbuseLimits = PERMISSIVE,
) -> AppDeps:
    gh = GitHubClient(base_url="https://api.github.com", transport=github.transport())
    preflight = PreflightService(github=gh, registry=RegistryClient(transport=registry.transport()))
    # TODO(T8): AppDeps has no `abuse` field until T8; drop this guard and pass abuse=abuse.
    extra: dict[str, Any] = {"abuse": abuse} if "abuse" in AppDeps.__dataclass_fields__ else {}
    return AppDeps(
        db=db,
        verifier=verifier,
        cipher=cipher,
        github=gh,
        preflight=preflight,
        clock=clock,
        **extra,
    )


@pytest.fixture
def app_deps(
    db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
) -> AppDeps:
    return build_deps(db, verifier, cipher, github, registry, clock)


@pytest.fixture
def client(app_deps: AppDeps) -> Iterator[TestClient]:
    with TestClient(create_app(app_deps)) as c:
        yield c


@pytest.fixture
def fresh_client(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
) -> Iterator[TestClient]:
    """API client over a private database, for capacity-sensitive tests."""
    deps = build_deps(fresh_db, verifier, cipher, github, registry, clock)
    with TestClient(create_app(deps)) as c:
        yield c


@pytest.fixture
def auth(signing_key: ec.EllipticCurvePrivateKey) -> Callable[..., dict[str, str]]:
    """Authorization header for a user (claim overrides allowed)."""

    def _auth(user: FakeUser, **overrides: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {mint(user.claims(**overrides), signing_key)}"}

    return _auth


# --- docker / network gating ---------------------------------------------------------------


def _docker_unavailable_reason() -> str | None:
    """None when `docker info` succeeds, otherwise why it does not."""
    if shutil.which("docker") is None:
        return "docker CLI not found"
    try:
        run = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker info failed: {exc}"
    if run.returncode != 0:
        return f"docker info failed: {run.stderr.strip()[:200]}"
    return None


def _gvisor_available() -> bool:
    if os.environ.get("SWF_SANDBOX_RUNTIME") != "runsc":
        return False
    try:
        run = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        return run.returncode == 0 and "runsc" in json.loads(run.stdout.strip() or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gvisor_items = [i for i in items if i.get_closest_marker("gvisor")]
    if gvisor_items and not _gvisor_available():
        skip_gv = pytest.mark.skip(
            reason="gVisor unavailable (SWF_SANDBOX_RUNTIME!=runsc or runsc not in docker info)"
        )
        for item in gvisor_items:
            item.add_marker(skip_gv)
    docker_items = [i for i in items if i.get_closest_marker("docker")]
    if docker_items:
        reason = _docker_unavailable_reason()
        if reason is not None:
            skip = pytest.mark.skip(reason=f"Docker unavailable ({reason})")
            for item in docker_items:
                item.add_marker(skip)
    if os.environ.get("SWF_SKIP_NETWORK") == "1":
        skip_net = pytest.mark.skip(reason="SWF_SKIP_NETWORK=1")
        for item in items:
            if item.get_closest_marker("network"):
                item.add_marker(skip_net)


# --- egress proxy (docker + network tests) -------------------------------------------------


@pytest.fixture(scope="session")
def run_id() -> str:
    return RUN_ID


@pytest.fixture(scope="session")
def egress_config() -> EgressConfig:
    """Proxy and per-task networks are named per run (`swf-test-<run>-...`)."""
    return EgressConfig(
        proxy_name=f"swf-test-{RUN_ID}-egress", network_prefix=f"swf-test-{RUN_ID}-task-"
    )


@pytest.fixture(scope="session")
def egress(egress_config: EgressConfig) -> Iterator[EgressInfo]:
    """The running Squid proxy; tasks attach their own network with `attach_task_network`."""
    info = ensure_egress(egress_config)
    try:
        yield info
    finally:
        teardown_egress(egress_config)
