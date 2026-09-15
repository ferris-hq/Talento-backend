"""Supabase access-token verification.

The app signs users in with Supabase Auth and sends the session's access token as
`Authorization: Bearer <jwt>`. We verify it locally (no round trip to Supabase):

* Projects on asymmetric signing keys (ES256/RS256): public keys come from the project's JWKS
  endpoint and are cached by PyJWKClient.
* Projects still on the legacy shared secret (HS256): verified with SUPABASE_JWT_SECRET.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from anyio import to_thread
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

ASYMMETRIC_ALGORITHMS = ["ES256", "RS256"]

_bearer = HTTPBearer(auto_error=False)
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    role: str  # Supabase auth role: "authenticated" (or "service_role" for server keys)
    email: str | None
    phone: str | None
    claims: dict


def _jwks_client(url: str) -> jwt.PyJWKClient:
    client = _jwks_clients.get(url)
    if client is None:
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)
        _jwks_clients[url] = client
    return client


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def decode_access_token(token: str, settings: Settings) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("Malformed token") from exc

    algorithm = header.get("alg")
    options = {"require": ["exp", "sub", "aud", "iss"]}

    try:
        if algorithm == "HS256":
            if not settings.supabase_jwt_secret:
                raise _unauthorized("Unsupported token algorithm")
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience=settings.supabase_jwt_audience,
                issuer=settings.supabase_issuer,
                options=options,
            )

        if algorithm not in ASYMMETRIC_ALGORITHMS:
            raise _unauthorized("Unsupported token algorithm")

        # PyJWKClient uses blocking urllib; keep it off the event loop.
        client = _jwks_client(settings.supabase_jwks_url)
        signing_key = await to_thread.run_sync(client.get_signing_key_from_jwt, token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=ASYMMETRIC_ALGORITHMS,
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_issuer,
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Token expired") from exc
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
        raise _unauthorized("Invalid token") from exc


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Missing bearer token")

    claims = await decode_access_token(credentials.credentials, settings)
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise _unauthorized("Invalid subject") from exc

    return AuthUser(
        id=user_id,
        role=claims.get("role", "authenticated"),
        email=claims.get("email") or None,
        phone=claims.get("phone") or None,
        claims=claims,
    )


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
