"""Production entry point: `uv run uvicorn swf.api.main:create_app_from_env --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from swf.api.app import AppDeps, create_app
from swf.auth import JwtVerifier, jwks_resolver
from swf.db import Database
from swf.github import GitHubClient
from swf.images import RegistryClient
from swf.preflight import PreflightService
from swf.settings import Settings
from swf.tokens import TokenCipher


def create_app_from_env() -> FastAPI:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    github = GitHubClient(base_url=settings.github_api_url)
    deps = AppDeps(
        db=db,
        verifier=JwtVerifier(
            issuer=settings.jwt_issuer,
            key_resolver=jwks_resolver(settings.supabase_url),
            hs256_secret=settings.supabase_jwt_secret,
        ),
        cipher=TokenCipher(settings.token_encryption_key),
        github=github,
        preflight=PreflightService(github=github, registry=RegistryClient()),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        db.open()
        try:
            yield
        finally:
            db.close()

    return create_app(deps, lifespan=lifespan)
