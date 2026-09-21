"""Admin endpoints against a real Postgres (R2 faked). Needs TEST_DATABASE_URL."""

import datetime as dt
import os
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import asyncpg
import httpx
import pytest

from app.db import create_pool
from app.main import create_app
from app.services import storage

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="needs TEST_DATABASE_URL")
TODAY = dt.date.today()


@pytest.fixture(autouse=True)
def fake_r2(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    deleted: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        storage, "delete_objects", lambda bucket, keys: deleted.append((bucket, keys))
    )
    return deleted


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
    await pool.execute("delete from auth.users where email like '%@admin.t.dev'")
    await pool.close()


async def make_user(pool: asyncpg.Pool, role: str, name: str = "Test") -> str:
    user_id = uuid4()
    await pool.execute(
        "insert into auth.users (id, email) values ($1, $2)", user_id, f"{user_id}@admin.t.dev"
    )
    await pool.execute(
        "update public.profiles set role = $2::public.user_role, full_name = $3, sport = 'football'"
        " where id = $1",
        user_id,
        role,
        name,
    )
    if role == "coach":
        await pool.execute(
            """insert into public.coach_profiles
                 (profile_id, organization_text, verification_status)
               values ($1, 'Accra Lions', 'pending')""",
            user_id,
        )
    if role == "athlete":
        await pool.execute("insert into public.athlete_profiles (profile_id) values ($1)", user_id)
    return str(user_id)


async def test_only_admins_get_in(env, make_token: Callable[..., str]) -> None:
    client, pool = env
    athlete = await make_user(pool, "athlete")
    coach = await make_user(pool, "coach")
    for user in (athlete, coach):
        r = await client.get(
            "/v1/admin/overview", headers={"Authorization": f"Bearer {make_token(user)}"}
        )
        assert r.status_code == 403, r.text
    assert (await client.get("/v1/admin/overview")).status_code == 401


async def test_coach_verification_and_trials(env, make_token) -> None:
    client, pool = env
    admin = await make_user(pool, "admin", "Admin")
    coach = await make_user(pool, "coach", "Coach Kwesi")
    auth = {"Authorization": f"Bearer {make_token(admin)}"}
    await pool.execute(
        "insert into public.coach_verification_requests (coach_id, notes)"
        " values ($1, 'My club ID')",
        coach,
    )
    trial_id = uuid4()
    await pool.execute(
        """insert into public.trials (id, created_by, club_name, title, sport, venue, trial_date,
                                      application_deadline)
           values ($1, $2, 'Accra Lions', 'Open trial', 'football', 'Accra', $3, $4)""",
        trial_id,
        coach,
        TODAY + dt.timedelta(days=10),
        TODAY + dt.timedelta(days=5),
    )

    r = await client.get("/v1/admin/coaches", headers=auth)
    assert r.status_code == 200
    pending = [c for c in r.json() if c["id"] == coach]
    assert pending and pending[0]["request_note"] == "My club ID" and pending[0]["trials"] == 1

    # approve
    r = await client.post(
        f"/v1/admin/coaches/{coach}/verification", headers=auth, json={"status": "verified"}
    )
    assert r.status_code == 200 and r.json()["verification_status"] == "verified"
    assert (
        await pool.fetchval(
            "select status::text from public.coach_verification_requests where coach_id = $1", coach
        )
        == "approved"
    )

    # rejecting later closes their open trials
    r = await client.post(
        f"/v1/admin/coaches/{coach}/verification",
        headers=auth,
        json={"status": "rejected", "note": "Could not confirm the club"},
    )
    assert r.status_code == 200 and r.json()["verified_at"] is None
    assert (
        await pool.fetchval("select status::text from public.trials where id = $1", trial_id)
        == "closed"
    )

    # admin can cancel a trial; applicants are told
    athlete = await make_user(pool, "athlete")
    await pool.execute(
        "insert into public.trial_applications (trial_id, athlete_id) values ($1, $2)",
        trial_id,
        athlete,
    )
    r = await client.post(f"/v1/admin/trials/{trial_id}/cancel", headers=auth, json={})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    row = await pool.fetchrow(
        "select status::text as s from public.trial_applications where trial_id = $1", trial_id
    )
    assert row["s"] == "rejected"

    r = await client.get(f"/v1/admin/trials/{trial_id}/applicants", headers=auth)
    assert r.status_code == 200 and len(r.json()) == 1


async def test_users_suspend_and_delete(env, make_token, fake_r2) -> None:
    client, pool = env
    admin = await make_user(pool, "admin", "Admin")
    athlete = await make_user(pool, "athlete", "Kofi Asante")
    auth = {"Authorization": f"Bearer {make_token(admin)}"}
    await pool.execute(
        """insert into public.videos (owner_id, title, status, share_to_feed, raw_key, playback_key,
                                      poster_key)
           values ($1, 'Clip', 'ready', true, 'raw/x', 'v/x/720.mp4', 'v/x/poster.jpg')""",
        athlete,
    )

    r = await client.get("/v1/admin/users", headers=auth, params={"search": "Kofi"})
    assert r.status_code == 200 and r.json()[0]["videos"] == 1

    r = await client.get(f"/v1/admin/users/{athlete}", headers=auth)
    assert r.status_code == 200
    assert r.json()["videos_list"][0]["playback_url"].endswith("v/x/720.mp4")

    # suspend: sign-in blocked and clips leave the feed
    r = await client.post(
        f"/v1/admin/users/{athlete}/suspension", headers=auth, json={"suspended": True}
    )
    assert r.status_code == 200 and r.json()["suspended"] is True
    assert (
        await pool.fetchval("select share_to_feed from public.videos where owner_id = $1", athlete)
        is False
    )
    r = await client.post(
        f"/v1/admin/users/{athlete}/suspension", headers=auth, json={"suspended": False}
    )
    assert r.json()["suspended"] is False

    # admins can't suspend or delete themselves
    assert (
        await client.post(
            f"/v1/admin/users/{admin}/suspension", headers=auth, json={"suspended": True}
        )
    ).status_code == 400
    assert (await client.delete(f"/v1/admin/users/{admin}", headers=auth)).status_code == 400

    r = await client.delete(f"/v1/admin/users/{athlete}", headers=auth)
    assert r.status_code == 204
    assert await pool.fetchval("select count(*) from auth.users where id = $1", athlete) == 0
    deleted_keys = [key for _, keys in fake_r2 for key in keys]
    assert "raw/x" in deleted_keys and "v/x/720.mp4" in deleted_keys


async def test_video_takedown(env, make_token, fake_r2) -> None:
    client, pool = env
    admin = await make_user(pool, "admin", "Admin")
    athlete = await make_user(pool, "athlete")
    auth = {"Authorization": f"Bearer {make_token(admin)}"}
    video_id = await pool.fetchval(
        """insert into public.videos (owner_id, title, status, share_to_feed, raw_key, playback_key,
                                      poster_key, rating)
           values ($1, 'Bad clip', 'ready', true, 'raw/y', 'v/y/720.mp4', 'v/y/poster.jpg', 7)
           returning id""",
        athlete,
    )

    r = await client.get("/v1/admin/videos", headers=auth, params={"status": "ready"})
    assert r.status_code == 200 and any(v["id"] == str(video_id) for v in r.json())

    r = await client.post(
        f"/v1/admin/videos/{video_id}/takedown",
        headers=auth,
        json={"reason": "Someone else's footage"},
    )
    assert r.status_code == 204
    row = await pool.fetchrow(
        """select status::text as status, reject_reason, share_to_feed, playback_key, rating
             from public.videos where id = $1""",
        video_id,
    )
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "Someone else's footage"
    assert row["share_to_feed"] is False and row["playback_key"] is None and row["rating"] is None
    assert "v/y/720.mp4" in [key for _, keys in fake_r2 for key in keys]


async def test_reports_queue(env, make_token) -> None:
    client, pool = env
    admin = await make_user(pool, "admin", "Admin")
    athlete = await make_user(pool, "athlete", "Kofi Asante")
    reporter = await make_user(pool, "athlete", "Ama Reporter")
    auth = {"Authorization": f"Bearer {make_token(admin)}"}
    video_id = await pool.fetchval(
        """insert into public.videos
             (owner_id, title, status, share_to_feed, playback_key, poster_key)
           values ($1, 'Match highlights', 'ready', true, 'v/r/720.mp4', 'v/r/p.jpg')
           returning id""",
        athlete,
    )
    await pool.execute(
        """insert into public.reports (reporter_id, target_type, target_id, reason, details)
           values ($1, 'video', $2, 'Not their clip', 'This is televised footage.')""",
        reporter,
        video_id,
    )

    r = await client.get("/v1/admin/reports", headers=auth)
    assert r.status_code == 200, r.text
    report = r.json()[0]
    assert report["reason"] == "Not their clip"
    assert report["subject"] == "Match highlights"
    assert report["subject_owner"] == "Kofi Asante"
    assert report["reporter_name"] == "Ama Reporter"
    assert report["playback_url"].endswith("v/r/720.mp4")
    assert report["reports_on_subject"] == 1

    r = await client.post(
        f"/v1/admin/reports/{report['id']}/decision",
        headers=auth,
        json={"status": "dismissed", "note": "The athlete is in the clip."},
    )
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    still_open = await client.get("/v1/admin/reports", headers=auth, params={"status": "open"})
    assert still_open.json() == []

    athlete_token = {"Authorization": f"Bearer {make_token(athlete)}"}
    assert (await client.get("/v1/admin/reports", headers=athlete_token)).status_code == 403


async def test_delete_my_own_account(env, make_token, fake_r2) -> None:
    client, pool = env
    athlete = await make_user(pool, "athlete", "Leaving Soon")
    await pool.execute(
        """insert into public.videos (owner_id, title, status, share_to_feed, raw_key, playback_key,
                                      poster_key)
           values ($1, 'Clip', 'ready', true, 'raw/me', 'v/me/720.mp4', 'v/me/poster.jpg')""",
        athlete,
    )

    assert (await client.delete("/v1/me")).status_code == 401

    r = await client.delete("/v1/me", headers={"Authorization": f"Bearer {make_token(athlete)}"})
    assert r.status_code == 204
    assert await pool.fetchval("select count(*) from auth.users where id = $1", athlete) == 0
    left = await pool.fetchval("select count(*) from public.videos where owner_id = $1", athlete)
    assert left == 0
    deleted_keys = [key for _, keys in fake_r2 for key in keys]
    assert "raw/me" in deleted_keys and "v/me/720.mp4" in deleted_keys
