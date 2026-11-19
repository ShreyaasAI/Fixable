"""Supabase JWT verification (plan M1; every /v1 endpoint requires a valid user JWT)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from helpers import ISSUER, FakeUser, mint

from swf.auth import AuthError, JwtVerifier

USER = FakeUser(id=uuid.UUID("11111111-1111-4111-8111-111111111111"), github_id=42, login="octo")


def test_valid_token_gives_principal(
    verifier: JwtVerifier, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    principal = verifier.verify(mint(USER.claims(), signing_key))
    assert principal.user_id == USER.id
    assert principal.github_login == "octo"
    assert principal.github_user_id == 42
    assert principal.claims["sub"] == str(USER.id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "anon"},
        {"iss": "https://evil.supabase.co/auth/v1"},
        {"role": "anon"},
        {"role": "service_role"},
        {"sub": "not-a-uuid"},
    ],
    ids=["expired", "wrong-aud", "wrong-iss", "anon-role", "service-role", "bad-sub"],
)
def test_rejects_bad_claims(
    verifier: JwtVerifier, signing_key: ec.EllipticCurvePrivateKey, overrides: dict[str, object]
) -> None:
    with pytest.raises(AuthError):
        verifier.verify(mint(USER.claims(**overrides), signing_key))


@pytest.mark.parametrize("missing", ["exp", "sub", "aud", "iss"])
def test_rejects_missing_required_claim(
    verifier: JwtVerifier, signing_key: ec.EllipticCurvePrivateKey, missing: str
) -> None:
    claims = USER.claims()
    del claims[missing]
    with pytest.raises(AuthError):
        verifier.verify(mint(claims, signing_key))


def test_rejects_signature_from_other_key(verifier: JwtVerifier) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(AuthError):
        verifier.verify(mint(USER.claims(), other))


def test_rejects_alg_none(verifier: JwtVerifier) -> None:
    token = jwt.encode(USER.claims(), key=None, algorithm="none")
    with pytest.raises(AuthError):
        verifier.verify(token)


def test_rejects_hs256_when_no_secret_configured(verifier: JwtVerifier) -> None:
    with pytest.raises(AuthError):
        verifier.verify(mint(USER.claims(), "x" * 32, alg="HS256"))


def test_accepts_hs256_with_legacy_secret() -> None:
    secret = "s" * 40
    v = JwtVerifier(issuer=ISSUER, key_resolver=None, hs256_secret=secret)
    assert v.verify(mint(USER.claims(), secret, alg="HS256")).user_id == USER.id
    with pytest.raises(AuthError):
        v.verify(mint(USER.claims(), "w" * 40, alg="HS256"))


def test_rejects_garbage(verifier: JwtVerifier) -> None:
    for token in ["", "abc", "a.b.c"]:
        with pytest.raises(AuthError):
            verifier.verify(token)


def test_key_resolver_errors_become_auth_errors(signing_key: ec.EllipticCurvePrivateKey) -> None:
    def broken(_token: str) -> object:
        raise jwt.PyJWKClientError("no matching kid")

    resolver: Callable[[str], object] = broken
    v = JwtVerifier(issuer=ISSUER, key_resolver=resolver)
    with pytest.raises(AuthError):
        v.verify(mint(USER.claims(), signing_key))
