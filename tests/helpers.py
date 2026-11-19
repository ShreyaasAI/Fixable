"""Shared test helpers. Fixtures live in conftest.py; plain helpers live here."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

ISSUER = "https://test-ref.supabase.co/auth/v1"


@dataclass(frozen=True)
class FakeUser:
    """A Supabase user signed in with GitHub (row exists in the shim's auth.users)."""

    id: uuid.UUID
    github_id: int
    login: str

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """Claims shaped like a Supabase access token for a GitHub sign-in."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": "authenticated",
            "role": "authenticated",
            "sub": str(self.id),
            "iat": now,
            "exp": now + 3600,
            "email": f"{self.login}@example.test",
            "app_metadata": {"provider": "github", "providers": ["github"]},
            "user_metadata": {
                "iss": "https://api.github.com",
                "provider_id": str(self.github_id),
                "sub": str(self.github_id),
                "user_name": self.login,
            },
        }
        claims.update(overrides)
        return claims


def mint(claims: dict[str, Any], key: Any, *, alg: str = "ES256") -> str:
    return jwt.encode(claims, key, algorithm=alg)


def repo_json(
    full_name: str, *, push: bool = True, private: bool = False, default_branch: str = "main"
) -> dict[str, Any]:
    return {
        "full_name": full_name,
        "private": private,
        "default_branch": default_branch,
        "permissions": {"admin": False, "push": push, "pull": True},
    }


@dataclass
class FakeGitHub:
    """In-memory GitHub REST API: /user and paginated /user/repos."""

    users: dict[str, tuple[int, str]] = field(default_factory=dict)  # token -> (id, login)
    repos: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # token -> repos
    page_size: int = 2
    next_link_override: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.users:
            return httpx.Response(401, json={"message": "Bad credentials"})
        gid, login = self.users[token]
        if request.url.path == "/user":
            return httpx.Response(
                200, json={"id": gid, "login": login}, headers={"X-OAuth-Scopes": "repo, read:user"}
            )
        if request.url.path == "/user/repos":
            page = int(request.url.params.get("page", "1"))
            items = self.repos.get(token, [])
            chunk = items[(page - 1) * self.page_size : page * self.page_size]
            headers = {}
            if page * self.page_size < len(items):
                nxt = self.next_link_override or str(request.url.copy_set_param("page", page + 1))
                headers["Link"] = f'<{nxt}>; rel="next"'
            return httpx.Response(200, json=chunk, headers=headers)
        return httpx.Response(404, json={"message": "Not Found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
