"""Supabase access-token verification.

Supabase signs user JWTs either with asymmetric signing keys (ES256/RS256, public keys at
`<project>/auth/v1/.well-known/jwks.json`) or, on legacy projects, with a shared HS256 secret.
The algorithm is chosen from an allow-list per key source, never trusted from the token alone.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

ASYMMETRIC_ALGS = ("ES256", "RS256")
AUDIENCE = "authenticated"

KeyResolver = Callable[[str], Any]


class AuthError(Exception):
    """The token is missing, malformed, expired, or not a signed-in user's token."""


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    claims: Mapping[str, Any]
    github_login: str | None
    github_user_id: int | None


def jwks_resolver(supabase_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class JwtVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        key_resolver: KeyResolver | None,
        hs256_secret: str | None = None,
        leeway_seconds: int = 0,
    ) -> None:
        self._issuer = issuer
        self._resolver = key_resolver
        self._secret = hs256_secret
        self._leeway = leeway_seconds

    def verify(self, token: str) -> Principal:
        try:
            alg = jwt.get_unverified_header(token).get("alg")
            key: Any
            if alg == "HS256" and self._secret:
                key = self._secret
            elif alg in ASYMMETRIC_ALGS and self._resolver is not None:
                key = self._resolver(token)
            else:
                raise AuthError(f"unsupported token algorithm: {alg!r}")
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise AuthError(str(exc)) from exc

        if claims.get("role") != "authenticated":
            raise AuthError("not a signed-in user token")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise AuthError("sub is not a UUID") from exc

        meta = claims.get("user_metadata")
        meta = meta if isinstance(meta, dict) else {}
        login = meta.get("user_name")
        provider_id = str(meta.get("provider_id", ""))
        return Principal(
            user_id=user_id,
            claims=claims,
            github_login=login if isinstance(login, str) and login else None,
            github_user_id=int(provider_id) if provider_id.isdigit() else None,
        )
