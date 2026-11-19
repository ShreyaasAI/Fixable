"""Minimal GitHub REST client using the user's OAuth token (backend only).

M1: identify the token's owner and list repos. Clone/push/PR arrive in M6.
The token is only ever sent to the configured API host; pagination links pointing elsewhere are
refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

USER_AGENT = "swf-agent"


class GitHubError(Exception):
    pass


class GitHubAuthError(GitHubError):
    """GitHub rejected the token (revoked, expired or wrong)."""


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Repo:
    full_name: str
    default_branch: str
    private: bool
    can_push: bool


class GitHubClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        max_pages: int = 10,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base = httpx.URL(base_url)
        self._transport = transport
        self._max_pages = max_pages
        self._timeout = timeout_seconds

    def _client(self, token: str) -> httpx.Client:
        return httpx.Client(
            base_url=self._base,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub API error {resp.status_code} for {resp.request.url.path}")

    def get_user(self, token: str) -> GitHubUser:
        with self._client(token) as client:
            resp = client.get("/user")
        self._check(resp)
        data = resp.json()
        raw_scopes = resp.headers.get("X-OAuth-Scopes", "")
        scopes = tuple(s.strip() for s in raw_scopes.split(",") if s.strip())
        return GitHubUser(id=int(data["id"]), login=str(data["login"]), scopes=scopes)

    def list_repos(self, token: str) -> list[Repo]:
        repos: list[Repo] = []
        url: httpx.URL | None = self._base.join("/user/repos").copy_merge_params(
            {"per_page": 100, "sort": "updated"}
        )
        with self._client(token) as client:
            for _ in range(self._max_pages):
                if url is None:
                    break
                if (url.scheme, url.host, url.port) != (
                    self._base.scheme,
                    self._base.host,
                    self._base.port,
                ):
                    raise GitHubError("refusing to follow a pagination link to another host")
                resp = client.get(url)
                self._check(resp)
                repos.extend(_repo(item) for item in resp.json())
                nxt = resp.links.get("next", {}).get("url")
                url = httpx.URL(nxt) if nxt else None
        return repos


def _repo(item: dict[str, Any]) -> Repo:
    perms = item.get("permissions") or {}
    return Repo(
        full_name=str(item["full_name"]),
        default_branch=str(item.get("default_branch") or "main"),
        private=bool(item.get("private")),
        can_push=bool(perms.get("push")),
    )
