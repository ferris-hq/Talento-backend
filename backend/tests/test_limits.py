"""Per-account rate limits: what they stop, and what they must never stop."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.deps import auth
from app.deps.limits import RateLimit


class FakeRedis:
    """Counts like Redis does, and can be told to fail."""

    def __init__(self, broken: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self.broken = broken

    async def incr(self, key: str) -> int:
        if self.broken:
            raise ConnectionError("redis is down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


@pytest.fixture
def client_for():
    """A one-route app behind the limiter, signed in as whoever the test asks for."""

    def build(redis: Any, limit: int = 2, user_id: str | None = None) -> TestClient:
        who = auth.AuthUser(
            id=UUID(user_id) if user_id else uuid4(),
            role="authenticated",
            email="tester@talento.test",
            phone=None,
            claims={},
        )

        app = FastAPI()
        app.state.queue = redis
        limiter = RateLimit("test", limit=limit, window_s=3600)

        @app.get("/limited")
        async def limited(request: Request) -> dict[str, str]:
            await limiter(request, who)
            return {"ok": "yes"}

        return TestClient(app)

    return build


def test_allows_up_to_the_limit_then_refuses(client_for) -> None:
    client = client_for(FakeRedis(), limit=2)

    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200

    blocked = client.get("/limited")
    assert blocked.status_code == 429
    assert "short time" in blocked.json()["detail"]


def test_counts_each_account_separately(client_for) -> None:
    redis = FakeRedis()
    first = client_for(redis, limit=1, user_id=str(uuid4()))
    second = client_for(redis, limit=1, user_id=str(uuid4()))

    assert first.get("/limited").status_code == 200
    assert first.get("/limited").status_code == 429
    assert second.get("/limited").status_code == 200, "one busy account doesn't block another"


def test_lets_requests_through_when_redis_is_down(client_for) -> None:
    """A limiter that can't count shouldn't be why an athlete can't upload."""
    client = client_for(FakeRedis(broken=True), limit=1)

    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200


def test_does_nothing_without_a_queue(client_for) -> None:
    client = client_for(None, limit=1)
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
