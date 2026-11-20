"""Shared test helpers. Fixtures live in conftest.py; plain helpers live here."""

from __future__ import annotations

import base64
import re
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
    repo_host: FakeRepoHost = field(default_factory=lambda: FakeRepoHost())

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
        repo_resp = self.repo_host.handle(request)
        if repo_resp is not None:
            return repo_resp
        return httpx.Response(404, json={"message": "Not Found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


DEFAULT_AGENTS_MD = """# Widgets

Run the tests with pytest.

```yaml agent-config
image: python:3.12-slim
setup: []
test: python -m pytest -q
resources:
  memory_mb: 512
  cpus: 0.5
```
"""


def agents_md(**resources: object) -> str:
    """AGENTS.md text with the given resource hints (image may be overridden too)."""
    image = resources.pop("image", "python:3.12-slim")
    lines = "\n".join(f"  {k}: {v}" for k, v in resources.items())
    block = f"resources:\n{lines}\n" if lines else ""
    return (
        f"# Repo\n\n```yaml agent-config\nimage: {image}\ntest: python -m pytest -q\n{block}```\n"
    )


@dataclass
class FakeRepoHost:
    """GitHub repo endpoints used by pre-flight: repo metadata, branch, AGENTS.md contents.

    Every repo exists with branch names other than `missing_branches`, unless listed in
    `missing_repos`. AGENTS.md defaults to DEFAULT_AGENTS_MD; map a repo to None for "absent".
    """

    agents: dict[str, str | None] = field(default_factory=dict)
    sizes_kb: dict[str, int] = field(default_factory=dict)
    missing_repos: set[str] = field(default_factory=set)
    missing_branches: set[str] = field(default_factory=set)
    readonly_repos: set[str] = field(default_factory=set)  # the token cannot push

    def handle(self, request: httpx.Request) -> httpx.Response | None:
        parts = request.url.path.split("/")
        if len(parts) < 4 or parts[1] != "repos":
            return None
        full = f"{parts[2]}/{parts[3]}"
        rest = parts[4:]
        if full in self.missing_repos:
            return httpx.Response(404, json={"message": "Not Found"})
        if not rest:
            return httpx.Response(
                200,
                json={
                    **repo_json(full, push=full not in self.readonly_repos),
                    "size": self.sizes_kb.get(full, 120),
                },
            )
        if rest[0] == "branches":
            branch = "/".join(rest[1:])
            if branch in self.missing_branches:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": branch})
        if rest[:2] == ["contents", "AGENTS.md"]:
            text = self.agents.get(full, DEFAULT_AGENTS_MD)
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            encoded = base64.b64encode(text.encode()).decode()
            return httpx.Response(
                200, json={"type": "file", "encoding": "base64", "content": encoded}
            )
        return httpx.Response(404, json={"message": "Not Found"})


# --- container registry -----------------------------------------------------------------

INDEX_MT = "application/vnd.oci.image.index.v1+json"
LIST_MT = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_MT = "application/vnd.docker.distribution.manifest.v2+json"


def index_manifest(*platforms: str, media_type: str = INDEX_MT) -> dict[str, Any]:
    """Multi-arch index; platforms like 'linux/amd64', 'linux/arm64/v8', 'unknown/unknown'."""
    manifests = []
    for i, p in enumerate(platforms):
        os_, arch, *variant = p.split("/")
        plat: dict[str, str] = {"os": os_, "architecture": arch}
        if variant:
            plat["variant"] = variant[0]
        manifests.append({"digest": f"sha256:{i:064d}", "platform": plat})
    return {"schemaVersion": 2, "mediaType": media_type, "manifests": manifests}


def single_manifest(config_digest: str) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": MANIFEST_MT,
        "config": {"digest": config_digest, "mediaType": "application/json"},
    }


@dataclass
class FakeRegistry:
    """OCI distribution API with the anonymous bearer-token challenge Docker Hub uses."""

    # (registry host, repository, tag-or-digest) -> manifest JSON
    manifests: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    # (registry host, repository, digest) -> config blob JSON
    blobs: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    token: str = "anon-token"
    # (registry host, repository, digest) blobs answered with 307 to the CDN host, as Docker Hub
    # does; the CDN serves the same blob without any registry authentication.
    redirect_blobs: set[tuple[str, str, str]] = field(default_factory=set)
    cdn_host: str = "cdn.registry.test"
    cdn_location_override: str | None = None  # e.g. an http:// URL, or a manifest path

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "auth.registry.test":
            return httpx.Response(200, json={"token": self.token})
        if host == self.cdn_host:
            digest = request.url.path.rsplit("/", 1)[-1]
            for (_h, _r, d), body in self.blobs.items():
                if d == digest:
                    return httpx.Response(200, json=body)
            return httpx.Response(404)
        m = re.fullmatch(r"/v2/(.+)/(manifests|blobs)/([^/]+)", request.url.path)
        if m is None:
            return httpx.Response(404)
        repo, kind, ref = m.groups()
        if request.headers.get("authorization") != f"Bearer {self.token}":
            challenge = (
                'Bearer realm="https://auth.registry.test/token",'
                f'service="{host}",scope="repository:{repo}:pull"'
            )
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        if kind == "blobs" and (host, repo, ref) in self.redirect_blobs:
            location = self.cdn_location_override or f"https://{self.cdn_host}/blobs/{ref}"
            return httpx.Response(307, headers={"Location": location})
        store = self.manifests if kind == "manifests" else self.blobs
        body = store.get((host, repo, ref))
        if body is None:
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        mt = body.get("mediaType", "application/json")
        return httpx.Response(200, json=body, headers={"Content-Type": mt})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
