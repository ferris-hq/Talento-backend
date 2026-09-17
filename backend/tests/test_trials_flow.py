"""Trial posting with flyers against a real Postgres (R2 faked). Needs TEST_DATABASE_URL."""

import datetime as dt
import io
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from PIL import Image

from app.db import create_pool
from app.main import create_app
from app.services import storage

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="needs TEST_DATABASE_URL")
TODAY = dt.date.today()


@pytest.fixture
def r2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "r2"

    def path(bucket: str, key: str) -> Path:
        return root / bucket / key

    def size(bucket: str, key: str):
        p = path(bucket, key)
        return p.stat().st_size if p.exists() else None

    def put_bytes(bucket: str, key: str, body: bytes, content_type: str) -> None:
        p = path(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)

    def delete(bucket: str, keys: list[str]) -> None:
        for key in keys:
            if key:
                path(bucket, key).unlink(missing_ok=True)

    monkeypatch.setattr(storage, "presigned_put", lambda b, k, ct: f"https://r2.test/{b}/{k}")
    monkeypatch.setattr(storage, "object_size", size)
    monkeypatch.setattr(storage, "put_bytes", put_bytes)
    monkeypatch.setattr(storage, "delete_objects", delete)
    monkeypatch.setattr(
        storage, "download_file", lambda b, k, dest: Path(dest).write_bytes(path(b, k).read_bytes())
    )
    return root


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
    await pool.execute("delete from auth.users where email like '%@trials.t.dev'")
    await pool.close()


async def make_coach(pool: asyncpg.Pool, verified: bool) -> str:
    user_id = uuid4()
    await pool.execute(
        "insert into auth.users (id, email) values ($1, $2)", user_id, f"{user_id}@trials.t.dev"
    )
    await pool.execute("update public.profiles set role = 'coach' where id = $1", user_id)
    await pool.execute(
        """insert into public.coach_profiles (profile_id, organization_text, verification_status)
           values ($1, 'Accra Lions Academy', $2::public.coach_verification_status)""",
        user_id,
        "verified" if verified else "pending",
    )
    return str(user_id)


def image_bytes(fmt: str, size=(1200, 1800), mode="RGB") -> bytes:
    buf = io.BytesIO()
    img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    exif = Image.Exif()
    exif[0x0110] = "Secret phone model"
    img.save(buf, fmt, **({"exif": exif} if fmt == "JPEG" else {}))
    return buf.getvalue()


def trial_body(**extra) -> dict:
    return {
        "title": "U-17 open trial",
        "sport": "football",
        "position": "Winger",
        "age_group": "U-17",
        "skill_level": "Intermediate",
        "description": "Bring boots.",
        "requirements": ["Age 15-17", "  ", "Boots"],
        "venue": "Accra Sports Stadium",
        "region": "Greater Accra",
        "trial_date": str(TODAY + dt.timedelta(days=14)),
        "start_time": "09:30",
        "application_deadline": str(TODAY + dt.timedelta(days=7)),
        "capacity": 40,
        **extra,
    }


async def upload_flyer(client, auth, r2: Path, data: bytes, content_type="image/jpeg") -> str:
    r = await client.post(
        "/v1/trials/flyer-uploads",
        headers=auth,
        json={"content_type": content_type, "size_bytes": len(data)},
    )
    assert r.status_code == 201, r.text
    target = r2 / r.json()["upload_url"].removeprefix("https://r2.test/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return r.json()["upload_id"]


async def test_verified_coach_posts_trial_with_flyer(env, r2, make_token: Callable[..., str]):
    client, pool = env
    coach = await make_coach(pool, verified=True)
    auth = {"Authorization": f"Bearer {make_token(coach)}"}

    upload_id = await upload_flyer(client, auth, r2, image_bytes("JPEG", (2400, 3600)))
    r = await client.post("/v1/trials", headers=auth, json=trial_body(flyer_upload_id=upload_id))
    assert r.status_code == 201, r.text
    trial = r.json()
    assert trial["club_name"] == "Accra Lions Academy"
    assert trial["requirements"] == ["Age 15-17", "Boots"]
    assert (trial["flyer_width"], trial["flyer_height"]) == (1067, 1600)
    key = trial["flyer_url"].removeprefix("https://media.talentoafrica.com/")
    stored = r2 / "talento-media" / key
    with Image.open(stored) as img:
        assert img.format == "JPEG" and not img.getexif()
    assert not any((r2 / "talento-raw").rglob("*.*")) and not any(
        p.is_file() for p in (r2 / "talento-raw").rglob("*")
    )

    # replace the flyer: the old file goes away
    new_upload = await upload_flyer(client, auth, r2, image_bytes("PNG", mode="RGBA"), "image/png")
    r = await client.patch(
        f"/v1/trials/{trial['id']}",
        headers=auth,
        json={"flyer_upload_id": new_upload, "status": "closed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed" and r.json()["flyer_url"] != trial["flyer_url"]
    assert not stored.exists()

    # no applicants: delete removes it entirely
    r = await client.delete(f"/v1/trials/{trial['id']}", headers=auth)
    assert r.status_code == 204
    assert await pool.fetchval("select count(*) from public.trials where id = $1", trial["id"]) == 0
    assert not any(p.is_file() for p in (r2 / "talento-media").rglob("*"))


async def test_trial_with_applicants_is_cancelled_not_deleted(env, r2, make_token):
    client, pool = env
    coach = await make_coach(pool, verified=True)
    auth = {"Authorization": f"Bearer {make_token(coach)}"}
    r = await client.post("/v1/trials", headers=auth, json=trial_body(age_group=None))
    trial_id = r.json()["id"]

    athlete = uuid4()
    await pool.execute(
        "insert into auth.users (id, email) values ($1, $2)", athlete, f"{athlete}@trials.t.dev"
    )
    await pool.execute(
        "insert into public.trial_applications (trial_id, athlete_id) values ($1, $2)",
        trial_id,
        athlete,
    )
    r = await client.delete(f"/v1/trials/{trial_id}", headers=auth)
    assert r.status_code == 204
    row = await pool.fetchrow(
        """select t.status::text as trial, a.status::text as application
             from public.trials t join public.trial_applications a on a.trial_id = t.id
            where t.id = $1""",
        trial_id,
    )
    assert (row["trial"], row["application"]) == ("cancelled", "rejected")
    r = await client.patch(f"/v1/trials/{trial_id}", headers=auth, json={"status": "open"})
    assert r.status_code == 409


async def test_rules(env, r2, make_token):
    client, pool = env
    unverified = await make_coach(pool, verified=False)
    r = await client.post(
        "/v1/trials",
        headers={"Authorization": f"Bearer {make_token(unverified)}"},
        json=trial_body(),
    )
    assert r.status_code == 403 and "verified" in r.json()["detail"]

    coach = await make_coach(pool, verified=True)
    auth = {"Authorization": f"Bearer {make_token(coach)}"}
    r = await client.post(
        "/v1/trials/flyer-uploads",
        headers=auth,
        json={"content_type": "video/mp4", "size_bytes": 1000},
    )
    assert r.status_code == 415 and "Videos aren't supported" in r.json()["detail"]
    r = await client.post(
        "/v1/trials/flyer-uploads",
        headers=auth,
        json={"content_type": "image/jpeg", "size_bytes": 50 * 1024 * 1024},
    )
    assert r.status_code == 413

    # a "jpeg" that is really something else
    upload_id = await upload_flyer(client, auth, r2, b"\x00\x00\x00 ftypmp42 not an image")
    r = await client.post("/v1/trials", headers=auth, json=trial_body(flyer_upload_id=upload_id))
    assert r.status_code == 422 and "couldn't read this image" in r.json()["detail"]
    assert (
        await pool.fetchval("select count(*) from public.trials where created_by = $1", coach) == 0
    )

    r = await client.post(
        "/v1/trials",
        headers=auth,
        json=trial_body(application_deadline=str(TODAY + dt.timedelta(days=30))),
    )
    assert r.status_code == 422
    r = await client.post("/v1/trials", headers=auth, json=trial_body(sport="cricket"))
    assert r.status_code == 422

    # someone else can't touch the trial
    r = await client.post("/v1/trials", headers=auth, json=trial_body())
    other = await make_coach(pool, verified=True)
    r2_ = await client.delete(
        f"/v1/trials/{r.json()['id']}", headers={"Authorization": f"Bearer {make_token(other)}"}
    )
    assert r2_.status_code == 404
