"""Runs the API against a real Postgres with all migrations applied.

Skipped unless TEST_DATABASE_URL points at such a database, e.g.

    createdb talento_it
    psql -d talento_it -f ../supabase/tests/local/auth_stub.sql
    for f in ../supabase/migrations/*.sql; do psql -d talento_it -f "$f"; done
    TEST_DATABASE_URL=postgresql:///talento_it uv run pytest tests/test_integration_db.py
"""

import os
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import asyncpg
import httpx
import pytest

from app.db import create_pool
from app.main import create_app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, asyncpg.Pool]]:
    assert DATABASE_URL
    app = create_app()
    pool = await create_pool(DATABASE_URL)
    app.state.db_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, pool
    await pool.close()


async def test_readyz_with_database(api) -> None:
    client, _ = api
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_me_reads_profile_created_by_signup_trigger(
    api, make_token: Callable[..., str]
) -> None:
    client, pool = api
    user_id = uuid4()
    async with pool.acquire() as db:
        await db.execute(
            "insert into auth.users (id, email, raw_user_meta_data) values ($1, $2, $3)",
            user_id,
            f"{user_id}@test.dev",
            '{"full_name": "Emmanuel Boateng"}',
        )
        await db.execute("update public.profiles set role = 'coach' where id = $1", user_id)
        await db.execute(
            "insert into public.coach_profiles (profile_id, organization_text, focus_sport)"
            " values ($1, 'Young Stars FC', 'football')",
            user_id,
        )

    try:
        response = await client.get(
            "/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["full_name"] == "Emmanuel Boateng"
        assert body["role"] == "coach"
        assert body["athlete"] is None
        assert body["coach"] == {
            "club_id": None,
            "organization_text": "Young Stars FC",
            "focus_sport": "football",
            "verification_status": "unverified",
        }
    finally:
        async with pool.acquire() as db:
            await db.execute("delete from auth.users where id = $1", user_id)
