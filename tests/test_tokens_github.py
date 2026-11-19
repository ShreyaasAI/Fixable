"""Token encryption at rest and the GitHub REST client (spec §4 item 2)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from helpers import FakeGitHub, repo_json

from swf.github import GitHubAuthError, GitHubClient, GitHubError
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
