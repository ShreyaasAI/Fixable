"""Shared fixtures.

DB tests run against a throwaway `postgres:17-alpine` container (or `SWF_TEST_DATABASE_URL`,
which must point at an empty database). The Supabase shim is applied first, then every file in
`supabase/migrations/` in order. Without Docker the DB tests are skipped and the skip is reported
(`-rs` in addopts).
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
from helpers import ISSUER, FakeGitHub, FakeUser, mint

from swf.api.app import AppDeps, create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.github import GitHubClient
from swf.tokens import TokenCipher

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "tests" / "fixtures" / "supabase_shim.sql"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
PG_IMAGE = "postgres:17-alpine"


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


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    external = os.environ.get("SWF_TEST_DATABASE_URL")
    cid: str | None = None
    if external:
        url = external
    else:
        cid, url = _start_postgres()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(SHIM.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            for migration in MIGRATIONS:
                conn.execute(migration.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        yield url
    finally:
        if cid:
            subprocess.run(["docker", "stop", cid], capture_output=True)


@pytest.fixture(scope="session")
def db(postgres_url: str) -> Iterator[Database]:
    database = Database(postgres_url)
    database.open()
    yield database
    database.close()


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
def app_deps(
    db: Database, verifier: JwtVerifier, cipher: TokenCipher, github: FakeGitHub, clock: Clock
) -> AppDeps:
    gh = GitHubClient(base_url="https://api.github.com", transport=github.transport())
    return AppDeps(db=db, verifier=verifier, cipher=cipher, github=gh, clock=clock)


@pytest.fixture
def client(app_deps: AppDeps) -> Iterator[TestClient]:
    with TestClient(create_app(app_deps)) as c:
        yield c


@pytest.fixture
def auth(signing_key: ec.EllipticCurvePrivateKey) -> Callable[..., dict[str, str]]:
    """Authorization header for a user (claim overrides allowed)."""

    def _auth(user: FakeUser, **overrides: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {mint(user.claims(**overrides), signing_key)}"}

    return _auth
