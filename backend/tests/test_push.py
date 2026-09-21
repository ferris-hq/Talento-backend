"""The push sweeper: what it sends, what it marks, and how it handles dead devices."""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from worker import push


class FakePool:
    """Just enough asyncpg.Pool for the sweeper: canned rows and recorded writes."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return self.rows

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((query, args))

    def writes(self, contains: str) -> list[tuple[str, tuple[Any, ...]]]:
        """Calls whose SQL starts with `contains` — so a select mentioning it doesn't match."""
        return [call for call in self.calls if call[0].lstrip().startswith(contains)]


def row(tokens: list[str], title: str = "You're in") -> dict[str, Any]:
    return {
        "id": uuid4(),
        "title": title,
        "body": "Accra Lions accepted your application.",
        "data": json.dumps({"href": "/trial/123"}),
        "created_at": None,
        "tokens": tokens,
    }


@pytest.fixture
def expo(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Records the messages posted to Expo; returns "ok" tickets for each."""
    sent: list[list[dict[str, Any]]] = []

    async def fake_post(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sent.append(messages)
        return [{"status": "ok"} for _ in messages]

    monkeypatch.setattr(push, "_post", fake_post)
    return sent


async def test_sends_one_message_per_device(expo) -> None:
    pool = FakePool([row(["ExponentPushToken[a]", "ExponentPushToken[b]"])])

    assert await push.send_pending_pushes({"db": pool}) == 1

    (messages,) = expo
    assert [m["to"] for m in messages] == ["ExponentPushToken[a]", "ExponentPushToken[b]"]
    assert messages[0]["title"] == "You're in"
    assert messages[0]["data"] == {"href": "/trial/123"}
    assert pool.writes("update public.notifications"), "the notification is marked as pushed"


async def test_marks_sent_even_without_a_device(expo) -> None:
    """Someone who never opened the app on a phone still shouldn't be retried forever."""
    pool = FakePool([row([])])

    assert await push.send_pending_pushes({"db": pool}) == 1
    assert expo == [], "nothing is posted to Expo"
    assert pool.writes("update public.notifications")


async def test_forgets_devices_expo_says_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"status": "ok"},
            {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        ]

    monkeypatch.setattr(push, "_post", fake_post)
    pool = FakePool([row(["ExponentPushToken[live]", "ExponentPushToken[dead]"])])

    await push.send_pending_pushes({"db": pool})

    deletes = pool.writes("delete from public.push_tokens")
    assert deletes and deletes[0][1][0] == ["ExponentPushToken[dead]"]


async def test_keeps_rows_for_a_later_try_when_expo_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise httpx.ConnectError("expo unreachable")

    monkeypatch.setattr(push, "_post", fake_post)
    pool = FakePool([row(["ExponentPushToken[a]"])])

    assert await push.send_pending_pushes({"db": pool}) == 0
    # Nothing is marked sent, so the next run tries again.
    assert not pool.writes("update public.notifications")


async def test_does_nothing_when_there_is_nothing_to_send(expo) -> None:
    pool = FakePool([])
    assert await push.send_pending_pushes({"db": pool}) == 0
    assert expo == []
