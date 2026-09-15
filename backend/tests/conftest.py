import os
import time
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

# Configure before the app (and its cached settings) is imported.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_JWT_SECRET": "legacy-test-secret-at-least-32-bytes-long!",
        "DATABASE_URL": "",
    }
)

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_connection
from app.deps import auth
from app.main import create_app

ISSUER = "https://test-project.supabase.co/auth/v1"


@pytest.fixture(scope="session")
def signing_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


class _FakeJwksClient:
    """Stands in for PyJWKClient: returns our test public key for the expected kid."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str) -> None:
        self._public_key = private_key.public_key()
        self._kid = kid

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != self._kid:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid}")
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self._public_key, as_dict=True)
        return jwt.PyJWK({**jwk, "kid": self._kid, "alg": "ES256"})


@pytest.fixture(autouse=True)
def fake_jwks(signing_key: ec.EllipticCurvePrivateKey) -> Iterator[None]:
    settings = get_settings()
    auth._jwks_clients[settings.supabase_jwks_url] = _FakeJwksClient(signing_key, "test-kid")  # type: ignore[assignment]
    yield
    auth._jwks_clients.clear()


@pytest.fixture
def make_token(signing_key: ec.EllipticCurvePrivateKey) -> Callable[..., str]:
    def _make(
        sub: UUID | str | None = None,
        *,
        expires_in: int = 3600,
        audience: str = "authenticated",
        issuer: str = ISSUER,
        kid: str = "test-kid",
        key: object | None = None,
        algorithm: str = "ES256",
        **extra: object,
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": str(sub or uuid4()),
            "aud": audience,
            "iss": issuer,
            "iat": now,
            "exp": now + expires_in,
            "role": "authenticated",
            "email": "athlete@test.dev",
            **extra,
        }
        return jwt.encode(
            claims,
            key if key is not None else signing_key,
            algorithm=algorithm,
            headers={"kid": kid},
        )

    return _make


class FakeConnection:
    """Minimal asyncpg.Connection double: returns a canned row for fetchrow."""

    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, query: str, *args: object) -> dict | None:
        self.calls.append((query, args))
        return self.row


@pytest.fixture
def fake_db() -> FakeConnection:
    return FakeConnection(row=None)


@pytest.fixture
def client(fake_db: FakeConnection) -> Iterator[TestClient]:
    app = create_app()

    async def _connection() -> Iterator[FakeConnection]:
        yield fake_db

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app) as test_client:
        yield test_client
