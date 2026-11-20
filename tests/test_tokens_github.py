"""Token encryption at rest and the GitHub REST client (spec §4 item 2)."""

from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet
from helpers import FakeGitHub, repo_json

from swf.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubFileTooLarge,
    GitHubNotFound,
    RepoMeta,
)
from swf.tokens import TokenCipher, TokenDecryptError


def test_cipher_roundtrip_and_no_plaintext() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    blob = cipher.encrypt("gho_secret123")
    assert "gho_secret123" not in blob
    assert cipher.decrypt(blob) == "gho_secret123"


def test_cipher_wrong_key_fails() -> None:
    blob = TokenCipher(Fernet.generate_key().decode()).encrypt("gho_x")
    with pytest.raises(TokenDecryptError):
        TokenCipher(Fernet.generate_key().decode()).decrypt(blob)


def test_cipher_rejects_bad_key() -> None:
    with pytest.raises(ValueError):
        TokenCipher("not-a-fernet-key")


def _client(gh: FakeGitHub) -> GitHubClient:
    return GitHubClient(base_url="https://api.github.com", transport=gh.transport())


def test_get_user_sends_bearer_and_parses_scopes() -> None:
    gh = FakeGitHub(users={"tok": (7, "octo")})
    user = _client(gh).get_user("tok")
    assert (user.id, user.login) == (7, "octo")
    assert user.scopes == ("repo", "read:user")
    assert gh.requests[0].headers["authorization"] == "Bearer tok"


def test_get_user_bad_token() -> None:
    with pytest.raises(GitHubAuthError):
        _client(FakeGitHub()).get_user("nope")


def test_list_repos_follows_pagination() -> None:
    repos = [repo_json(f"o/r{i}", push=i % 2 == 0) for i in range(5)]
    gh = FakeGitHub(users={"tok": (7, "octo")}, repos={"tok": repos}, page_size=2)
    got = _client(gh).list_repos("tok")
    assert [r.full_name for r in got] == [f"o/r{i}" for i in range(5)]
    assert [r.can_push for r in got] == [True, False, True, False, True]
    assert len(gh.requests) == 3


def test_list_repos_page_cap() -> None:
    repos = [repo_json(f"o/r{i}") for i in range(10)]
    gh = FakeGitHub(users={"tok": (7, "octo")}, repos={"tok": repos}, page_size=1)
    client = GitHubClient(base_url="https://api.github.com", transport=gh.transport(), max_pages=3)
    assert len(client.list_repos("tok")) == 3


def test_list_repos_never_sends_token_to_other_host() -> None:
    repos = [repo_json(f"o/r{i}") for i in range(3)]
    gh = FakeGitHub(
        users={"tok": (7, "octo")},
        repos={"tok": repos},
        page_size=1,
        next_link_override="https://evil.example/user/repos?page=2",
    )
    with pytest.raises(GitHubError):
        _client(gh).list_repos("tok")
    assert all(r.url.host == "api.github.com" for r in gh.requests)


# --- M2 additions: repo metadata, branches, file contents (contract §3) ---------------------

MiB = 1024 * 1024


def _gh_with_repo(**host: object) -> FakeGitHub:
    gh = FakeGitHub(users={"tok": (7, "octo")})
    for key, value in host.items():
        setattr(gh.repo_host, key, value)
    return gh


def test_get_repo_returns_metadata_including_size() -> None:
    gh = _gh_with_repo(sizes_kb={"octo/widgets": 4321})
    meta = _client(gh).get_repo("tok", "octo/widgets")
    assert meta == RepoMeta(
        full_name="octo/widgets",
        default_branch="main",
        private=False,
        can_push=True,
        size_kb=4321,
    )
    assert gh.requests[-1].url.path == "/repos/octo/widgets"
    assert gh.requests[-1].headers["authorization"] == "Bearer tok"


def test_get_repo_not_found() -> None:
    gh = _gh_with_repo(missing_repos={"octo/gone"})
    with pytest.raises(GitHubNotFound):
        _client(gh).get_repo("tok", "octo/gone")
    assert issubclass(GitHubNotFound, GitHubError)


def test_get_repo_bad_token() -> None:
    with pytest.raises(GitHubAuthError):
        _client(_gh_with_repo()).get_repo("wrong", "octo/widgets")


def test_branch_exists() -> None:
    gh = _gh_with_repo(missing_branches={"nope"})
    client = _client(gh)
    assert client.branch_exists("tok", "octo/widgets", "main") is True
    assert client.branch_exists("tok", "octo/widgets", "feature/x") is True
    assert client.branch_exists("tok", "octo/widgets", "nope") is False
    assert gh.requests[0].url.path == "/repos/octo/widgets/branches/main"


def test_branch_exists_bad_token() -> None:
    with pytest.raises(GitHubAuthError):
        _client(_gh_with_repo()).branch_exists("wrong", "octo/widgets", "main")


def test_get_file_decodes_content() -> None:
    text = "# Widgets\n\nUnicode: café ✓\n"
    gh = _gh_with_repo(agents={"octo/widgets": text})
    assert _client(gh).get_file("tok", "octo/widgets", "AGENTS.md", ref="dev") == text
    request = gh.requests[-1]
    assert request.url.path == "/repos/octo/widgets/contents/AGENTS.md"
    assert request.url.params["ref"] == "dev"


def test_get_file_missing_is_none() -> None:
    gh = _gh_with_repo(agents={"octo/widgets": None})
    assert _client(gh).get_file("tok", "octo/widgets", "AGENTS.md", ref="main") is None


def test_get_file_over_one_mib_is_an_error() -> None:
    gh = _gh_with_repo(agents={"octo/widgets": "x" * (MiB + 1)})
    with pytest.raises(GitHubFileTooLarge):
        _client(gh).get_file("tok", "octo/widgets", "AGENTS.md", ref="main")
    assert issubclass(GitHubFileTooLarge, GitHubError)


def test_get_file_exactly_one_mib_is_fine() -> None:
    gh = _gh_with_repo(agents={"octo/widgets": "x" * MiB})
    assert len(_client(gh).get_file("tok", "octo/widgets", "AGENTS.md", ref="main") or "") == MiB


def test_get_file_directory_is_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"type": "file", "name": "a.py"}])

    client = GitHubClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError):
        client.get_file("tok", "octo/widgets", "AGENTS.md", ref="main")


def test_get_file_bad_token() -> None:
    with pytest.raises(GitHubAuthError):
        _client(_gh_with_repo()).get_file("wrong", "octo/widgets", "AGENTS.md", ref="main")
