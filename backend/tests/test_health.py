import os
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import create_pool
from app.main import create_app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_without_database_is_unavailable(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["database"] == "not configured"


@pytest.fixture
async def env() -> AsyncIterator[tuple[httpx.AsyncClient, asyncpg.Pool]]:
    assert DATABASE_URL
    app = create_app()
    pool = await create_pool(DATABASE_URL)
    app.state.db_pool = pool
    app.state.queue = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, pool
    await pool.close()


@pytest.mark.skipif(not DATABASE_URL, reason="needs TEST_DATABASE_URL")
async def test_statusz_reports_work_in_flight(env) -> None:
    """The numbers an uptime monitor should page on."""
    client, pool = env
    owner = await pool.fetchval(
        "insert into auth.users (id, email)"
        " values (gen_random_uuid(), 'statusz@t.dev') returning id"
    )
    await pool.execute(
        """insert into public.videos (owner_id, title, status, raw_key, processing_started_at)
           values ($1, 'Fresh', 'processing', 'raw/fresh', now())""",
        owner,
    )

    r = await client.get("/statusz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["in_flight"] == 1
    assert body["stuck"] == 0

    # A clip stuck in processing means the worker is gone: say so loudly.
    await pool.execute(
        "update public.videos set processing_started_at = now() - interval '2 hours'"
        " where owner_id = $1",
        owner,
    )
    r = await client.get("/statusz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["stuck"] == 1

    await pool.execute("delete from auth.users where id = $1", owner)
