from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from tests.conftest import FakeConnection


def _profile_row(user_id, **overrides) -> dict:
    row = {
        "id": user_id,
        "role": "athlete",
        "full_name": "Kwam Asante",
        "avatar_key": None,
        "region": "Ashanti",
        "city": "Kumasi",
        "sport": "football",
        "onboarded_at": datetime(2026, 9, 15, tzinfo=UTC),
        "has_athlete": True,
        "position": "Forward",
        "age_group": "U-17",
        "is_minor": True,
        "guardian_consented": False,
        "visibility": "public",
        "has_coach": False,
        "club_id": None,
        "organization_text": None,
        "focus_sport": None,
        "verification_status": None,
    }
    row.update(overrides)
    return row


def test_me_requires_a_token(client: TestClient) -> None:
    response = client.get("/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_returns_athlete_profile(
    client: TestClient, fake_db: FakeConnection, make_token: Callable[..., str]
) -> None:
    user_id = uuid4()
    fake_db.row = _profile_row(user_id)

    response = client.get("/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user_id)
    assert body["email"] == "athlete@test.dev"
    assert body["role"] == "athlete"
    assert body["athlete"] == {
        "position": "Forward",
        "age_group": "U-17",
        "is_minor": True,
        "guardian_consented": False,
        "visibility": "public",
    }
    assert body["coach"] is None
    # The query is always scoped to the token's subject.
    assert fake_db.calls[0][1] == (user_id,)


def test_me_returns_coach_profile(
    client: TestClient, fake_db: FakeConnection, make_token: Callable[..., str]
) -> None:
    user_id = uuid4()
    fake_db.row = _profile_row(
        user_id,
        role="coach",
        has_athlete=False,
        has_coach=True,
        organization_text="Hearts of Oak Academy",
        focus_sport="football",
        verification_status="pending",
    )

    response = client.get("/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"})

    assert response.status_code == 200
    assert response.json()["athlete"] is None
    assert response.json()["coach"]["verification_status"] == "pending"


def test_me_missing_profile_is_404(client: TestClient, make_token: Callable[..., str]) -> None:
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {make_token()}"})
    assert response.status_code == 404


def test_legacy_hs256_secret_is_accepted(
    client: TestClient, fake_db: FakeConnection, make_token: Callable[..., str]
) -> None:
    user_id = uuid4()
    fake_db.row = _profile_row(user_id)
    token = make_token(user_id, key="legacy-test-secret-at-least-32-bytes-long!", algorithm="HS256")

    response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("token_kwargs", "detail"),
    [
        ({"expires_in": -60}, "Token expired"),
        ({"audience": "anon"}, "Invalid token"),
        ({"issuer": "https://evil.example.com/auth/v1"}, "Invalid token"),
        ({"kid": "unknown-kid"}, "Invalid token"),
        ({"sub": "not-a-uuid"}, "Invalid subject"),
        ({"key": "wrong-secret-but-still-32-bytes-long!!", "algorithm": "HS256"}, "Invalid token"),
    ],
)
def test_rejected_tokens(
    client: TestClient, make_token: Callable[..., str], token_kwargs: dict, detail: str
) -> None:
    token = make_token(**token_kwargs)
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == detail


def test_token_signed_by_another_key_is_rejected(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    response = client.get(
        "/v1/me", headers={"Authorization": f"Bearer {make_token(key=attacker_key)}"}
    )
    assert response.status_code == 401


def test_unsigned_token_is_rejected(client: TestClient) -> None:
    token = jwt.encode(
        {"sub": str(uuid4()), "aud": "authenticated", "iss": "x", "exp": 9999999999},
        key=None,
        algorithm="none",
    )
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Unsupported token algorithm"


def test_malformed_token_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
