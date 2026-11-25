"""M10b §0.2-0.3: deployment settings and the CORS allowlist."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

import httpx
import pytest
from conftest import Clock, build_deps
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry

from swf.api.app import create_app
from swf.api.main import build_app_deps
from swf.auth import JwtVerifier
from swf.db import Database
from swf.settings import Deployment, Settings, SettingsError
from swf.tokens import TokenCipher

SITE = "https://swf-web.vercel.app"

MakeClient = Callable[[tuple[str, ...]], TestClient]


# --- settings ---------------------------------------------------------------------------------


def test_unset_means_local_and_no_cors() -> None:
    d = Deployment.from_env({})
    assert d.public is False
    assert d.cors_origins == ()


def test_origins_are_split_trimmed_and_lose_a_trailing_slash() -> None:
    env = {"SWF_CORS_ORIGINS": f" {SITE}/ , http://localhost:5173,http://127.0.0.1:4173 "}
    assert Deployment.from_env(env).cors_origins == (
        SITE,
        "http://localhost:5173",
        "http://127.0.0.1:4173",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://swf.example/app",  # a path
        "http://swf.example",  # plain http off localhost
        "swf.example",  # no scheme
        "https://",
        "ftp://swf.example",
        "https://swf.example?x=1",
    ],
)
def test_bad_origins_are_refused(origin: str) -> None:
    with pytest.raises(SettingsError, match="SWF_CORS_ORIGINS"):
        Deployment.from_env({"SWF_CORS_ORIGINS": origin})


@pytest.mark.parametrize(
    ("raw", "public"), [("1", True), ("true", True), ("0", False), ("", False)]
)
def test_public_flag(raw: str, public: bool) -> None:
    env = {"SWF_PUBLIC": raw, "SWF_CORS_ORIGINS": SITE}
    assert Deployment.from_env(env).public is public


def test_a_bad_public_flag_is_refused() -> None:
    with pytest.raises(SettingsError, match="SWF_PUBLIC"):
        Deployment.from_env({"SWF_PUBLIC": "yes please", "SWF_CORS_ORIGINS": SITE})


def test_public_needs_origins() -> None:
    with pytest.raises(SettingsError, match="SWF_CORS_ORIGINS"):
        Deployment.from_env({"SWF_PUBLIC": "1"})


def test_build_app_deps_reads_the_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWF_CORS_ORIGINS", SITE)
    settings = Settings(
        database_url="postgresql://u:p@127.0.0.1:1/db",
        supabase_url="https://ref.supabase.co",
        token_encryption_key=Fernet.generate_key().decode(),
    )
    deps = build_app_deps(settings, Database(settings.database_url))
    assert deps.cors_origins == (SITE,)


# --- the middleware ---------------------------------------------------------------------------


@pytest.fixture
def make_client(
    fresh_db: Database,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
) -> Iterator[MakeClient]:
    clients: list[TestClient] = []

    def make(origins: tuple[str, ...]) -> TestClient:
        deps = replace(
            build_deps(fresh_db, verifier, cipher, github, registry, clock), cors_origins=origins
        )
        client = TestClient(create_app(deps))
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.close()


def preflight_request(client: TestClient, origin: str) -> httpx.Response:
    return client.options(
        "/v1/tasks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


def test_an_allowed_origin_passes_the_browser_preflight(make_client: MakeClient) -> None:
    client = make_client((SITE,))
    resp = preflight_request(client, SITE)
    assert resp.status_code == 200
    h = resp.headers
    assert h["access-control-allow-origin"] == SITE
    assert "authorization" in h["access-control-allow-headers"].lower()
    assert "POST" in h["access-control-allow-methods"]
    assert "DELETE" in h["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in h
    assert h["access-control-max-age"] == "600"


def test_the_sse_resume_header_is_allowed(make_client: MakeClient) -> None:
    client = make_client((SITE,))
    resp = client.options(
        "/v1/tasks/x/events",
        headers={
            "Origin": SITE,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,last-event-id",
        },
    )
    assert resp.status_code == 200
    assert "last-event-id" in resp.headers["access-control-allow-headers"].lower()


def test_simple_responses_carry_the_origin_and_expose_retry_after(make_client: MakeClient) -> None:
    client = make_client((SITE,))
    resp = client.get("/healthz", headers={"Origin": SITE})
    assert resp.headers["access-control-allow-origin"] == SITE
    assert "retry-after" in resp.headers["access-control-expose-headers"].lower()


def test_another_origin_gets_no_cors_headers(make_client: MakeClient) -> None:
    client = make_client((SITE,))
    resp = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
    bad = preflight_request(client, "https://evil.example")
    assert "access-control-allow-origin" not in bad.headers


def test_no_origins_means_no_cors_headers(make_client: MakeClient) -> None:
    client = make_client(())
    resp = client.get("/healthz", headers={"Origin": SITE})
    assert "access-control-allow-origin" not in resp.headers
