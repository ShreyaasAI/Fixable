"""Minimal GitHub REST client using the user's OAuth token (backend only).

M1: identify the token's owner and list repos. M2: repo metadata, branch check and `AGENTS.md`
contents for pre-flight (this is also the repo/branch access check). M6: open a pull request
(`create_pull`); there is deliberately no endpoint that changes a PR after it is opened.
The token is only ever sent to the configured API host; pagination links pointing elsewhere are
refused and redirects are never followed.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

USER_AGENT = "swf-agent"
MAX_FILE_BYTES = 1024 * 1024
_DELETABLE_BRANCH_RE = re.compile(r"^(?:agent/[0-9a-f-]{36}|seed/[0-9a-z-]{1,64})$", re.ASCII)


class GitHubError(Exception):
    pass


class GitHubAuthError(GitHubError):
    """GitHub rejected the token (revoked, expired or wrong)."""


class GitHubNotFound(GitHubError):
    """404: the resource does not exist or the token cannot see it (GitHub does not say which)."""


class GitHubUnprocessable(GitHubError):
    """422: for example "a pull request already exists"."""


class GitHubFileTooLarge(GitHubError):
    """`get_file` target is over MAX_FILE_BYTES; the message is shown to the user."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    html_url: str


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


@dataclass(frozen=True)
class RepoMeta:
    full_name: str
    default_branch: str
    private: bool
    can_push: bool
    size_kb: int


class GitHubClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        max_pages: int = 10,
        timeout_seconds: float = 15.0,
        html_base: str = "https://github.com/",
    ) -> None:
        self._base = httpx.URL(base_url)
        self._html_base = html_base
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

    def get_repo(self, token: str, full_name: str) -> RepoMeta:
        with self._client(token) as client:
            resp = client.get(f"/repos/{quote(full_name, safe='/')}")
        if resp.status_code == 404:
            raise GitHubNotFound(f"repository {full_name} not found")
        self._check(resp)
        data = resp.json()
        repo = _repo(data)
        return RepoMeta(
            full_name=repo.full_name,
            default_branch=repo.default_branch,
            private=repo.private,
            can_push=repo.can_push,
            size_kb=int(data.get("size") or 0),
        )

    def branch_exists(self, token: str, full_name: str, branch: str) -> bool:
        path = f"/repos/{quote(full_name, safe='/')}/branches/{quote(branch, safe='/')}"
        with self._client(token) as client:
            resp = client.get(path)
        if resp.status_code == 404:
            return False
        self._check(resp)
        return True

    def get_file(self, token: str, full_name: str, path: str, *, ref: str) -> str | None:
        """UTF-8 text of a file at `ref`, or None if absent; files over 1 MiB are refused."""
        url = f"/repos/{quote(full_name, safe='/')}/contents/{quote(path, safe='/')}"
        with self._client(token) as client:
            resp = client.get(url, params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._check(resp)
        data = resp.json()
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubError(f"{path} is not a regular file")
        size = data.get("size")
        if isinstance(size, int) and size > MAX_FILE_BYTES:
            raise GitHubFileTooLarge(f"{path} is larger than 1 MiB ({size} bytes)")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubError(f"{path} has no inline base64 content")
        try:
            raw = base64.b64decode(data["content"])
        except (binascii.Error, ValueError) as exc:
            raise GitHubError(f"{path} content is not valid base64") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise GitHubFileTooLarge(f"{path} is larger than 1 MiB ({len(raw)} bytes)")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubError(f"{path} is not UTF-8 text") from exc

    def create_pull(
        self, token: str, full_name: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """Open a ready-for-review PR (m6 §3)."""
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": False,
            "maintainer_can_modify": False,
        }
        with self._client(token) as client:
            resp = client.post(f"/repos/{quote(full_name, safe='/')}/pulls", json=payload)
        if resp.status_code == 404:
            raise GitHubNotFound(f"repository {full_name} not found")
        if resp.status_code == 422:
            raise GitHubUnprocessable(f"GitHub refused the pull request (422) for {full_name}")
        self._check(resp)
        data = resp.json()
        html_url = str(data.get("html_url") or "")
        if not html_url.startswith(self._html_base):
            raise GitHubError("pull request html_url is not on the expected host")
        return PullRequest(number=int(data["number"]), html_url=html_url)

    # --- M9 eval cleanup (m9-contract §1.5): close, never merge ------------------------------

    def _get_pull(self, token: str, full_name: str, number: int) -> dict[str, Any]:
        with self._client(token) as client:
            resp = client.get(f"/repos/{quote(full_name, safe='/')}/pulls/{int(number)}")
        if resp.status_code == 404:
            raise GitHubNotFound(f"pull request {number} not found")
        self._check(resp)
        data = resp.json()
        if not isinstance(data, dict):
            raise GitHubError("unexpected pull request payload")
        return data

    def close_pull(self, token: str, full_name: str, number: int) -> None:
        with self._client(token) as client:
            resp = client.patch(
                f"/repos/{quote(full_name, safe='/')}/pulls/{int(number)}",
                json={"state": "closed"},
            )
        self._check(resp)

    def delete_branch(self, token: str, full_name: str, branch: str) -> None:
        """Only `agent/<task-id>` or `seed/<name>` refs; anything else is refused unsent."""
        if not isinstance(branch, str) or not _DELETABLE_BRANCH_RE.fullmatch(branch):
            raise ValueError(f"refusing to delete branch {branch!r}")
        with self._client(token) as client:
            resp = client.delete(
                f"/repos/{quote(full_name, safe='/')}/git/refs/heads/{quote(branch, safe='/')}"
            )
        self._check(resp)


def _repo(item: dict[str, Any]) -> Repo:
    perms = item.get("permissions") or {}
    return Repo(
        full_name=str(item["full_name"]),
        default_branch=str(item.get("default_branch") or "main"),
        private=bool(item.get("private")),
        can_push=bool(perms.get("push")),
    )
