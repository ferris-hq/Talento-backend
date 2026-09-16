"""Upload -> complete -> worker -> ready, against a real Postgres (R2 and Redis are faked).

Needs TEST_DATABASE_URL (see test_integration_db.py) and ffmpeg.
"""

import os
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest

from app.db import create_pool
from app.main import create_app
from app.services import storage
from tests.test_media import make_clip
from worker import tasks

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or shutil.which("ffmpeg") is None, reason="needs TEST_DATABASE_URL and ffmpeg"
)


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name: str, *args, **kwargs):
        self.jobs.append((name, args, kwargs))

    async def aclose(self) -> None:
        pass


@pytest.fixture
def bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """R2 stand-in: objects are files under tmp_path/<bucket>/<key>."""
    root = tmp_path / "r2"

    def path(b: str, key: str) -> Path:
        return root / b / key

    def put_url(b: str, key: str, content_type: str) -> str:
        return f"https://r2.test/{b}/{key}?ct={content_type}"

    def size(b: str, key: str):
        p = path(b, key)
        return p.stat().st_size if p.exists() else None

    def delete(b: str, keys: list[str]) -> None:
        for key in keys:
            if key:
                path(b, key).unlink(missing_ok=True)

    def download(b: str, key: str, dest: Path) -> None:
        shutil.copy(path(b, key), dest)

    def upload(b: str, key: str, src: Path, content_type: str) -> None:
        p = path(b, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, p)

    monkeypatch.setattr(storage, "presigned_put", put_url)
    monkeypatch.setattr(storage, "object_size", size)
    monkeypatch.setattr(storage, "delete_objects", delete)
    monkeypatch.setattr(tasks, "_download", download)
    monkeypatch.setattr(tasks, "_upload", upload)
    return root


@pytest.fixture
async def env() -> AsyncIterator[tuple[httpx.AsyncClient, asyncpg.Pool, FakeQueue]]:
    assert DATABASE_URL
    app = create_app()
    pool = await create_pool(DATABASE_URL)
    queue = FakeQueue()
    app.state.db_pool = pool
    app.state.queue = queue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, pool, queue
    await pool.close()


async def make_user(pool: asyncpg.Pool, role: str) -> str:
    user_id = uuid4()
    await pool.execute(
        "insert into auth.users (id, email) values ($1, $2)", user_id, f"{user_id}@t.dev"
    )
    await pool.execute(
        "update public.profiles set role = $2::public.user_role, sport = 'football' where id = $1",
        user_id,
        role,
    )
    return str(user_id)


def put(bucket: Path, upload_url: str, src: Path) -> None:
    key_path = upload_url.removeprefix("https://r2.test/").split("?")[0]
    dest = bucket / key_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)


async def run_worker(pool: asyncpg.Pool, video_id: str) -> str:
    return await tasks.process_video({"db": pool, "job_try": 1}, video_id)


async def test_upload_process_publish_delete(
    env, bucket: Path, tmp_path: Path, make_token: Callable[..., str]
) -> None:
    client, pool, queue = env
    user = await make_user(pool, "athlete")
    auth = {"Authorization": f"Bearer {make_token(user)}"}
    try:
        clip = make_clip(tmp_path / "clip.mov", seconds=3, size="1080x1920")

        # 1. create
        r = await client.post(
            "/v1/uploads",
            headers=auth,
            json={
                "title": "Weak foot finishing",
                "caption": "Left foot only",
                "category": "Skills",
                "content_type": "video/quicktime",
                "size_bytes": clip.stat().st_size,
                "duration_s": 3,
            },
        )
        assert r.status_code == 201, r.text
        created = r.json()
        video_id = created["video_id"]
        assert created["headers"] == {"Content-Type": "video/quicktime"}

        # complete before the file exists -> 409
        r = await client.post(f"/v1/uploads/{video_id}/complete", headers=auth, json={})
        assert r.status_code == 409

        # 2. upload + 3. complete
        put(bucket, created["upload_url"], clip)
        r = await client.post(f"/v1/uploads/{video_id}/complete", headers=auth, json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "processing"
        assert queue.jobs == [("process_video", (video_id,), {"_job_id": f"process:{video_id}"})]

        # completing again is a no-op
        r = await client.post(f"/v1/uploads/{video_id}/complete", headers=auth, json={})
        assert r.json()["status"] == "processing" and len(queue.jobs) == 1

        # 4. worker
        assert await run_worker(pool, video_id) == "ready"
        r = await client.get("/v1/videos", headers=auth)
        (video,) = r.json()
        assert video["status"] == "ready"
        assert (video["width"], video["height"]) == (720, 1280)
        assert video["playback_url"] == f"https://media.talentoafrica.com/v/{video_id}/720.mp4"
        assert (bucket / "talento-media" / f"v/{video_id}/poster.jpg").exists()

        # someone else can't touch it
        other = await make_user(pool, "athlete")
        other_auth = {"Authorization": f"Bearer {make_token(other)}"}
        r = await client.delete(f"/v1/videos/{video_id}", headers=other_auth)
        assert r.status_code == 404

        # delete removes files and row
        r = await client.delete(f"/v1/videos/{video_id}", headers=auth)
        assert r.status_code == 204
        assert not (bucket / "talento-media" / f"v/{video_id}/720.mp4").exists()
        assert not any(p.is_file() for p in (bucket / "talento-raw").rglob("*"))
        assert (
            await pool.fetchval("select count(*) from public.videos where id = $1", video_id) == 0
        )
    finally:
        await pool.execute("delete from auth.users where email like '%@t.dev'")


async def test_draft_then_publish_respects_quota(
    env, bucket: Path, tmp_path: Path, make_token: Callable[..., str], monkeypatch
) -> None:
    client, pool, _ = env
    user = await make_user(pool, "athlete")
    auth = {"Authorization": f"Bearer {make_token(user)}"}
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_library_videos", 1)
    try:
        clip = make_clip(tmp_path / "clip.mp4", seconds=3, size="640x360", audio=False)
        body = {"title": "Clip", "size_bytes": clip.stat().st_size, "content_type": "video/mp4"}

        async def upload(draft: bool) -> str:
            r = await client.post("/v1/uploads", headers=auth, json={**body, "draft": draft})
            assert r.status_code == 201, r.text
            put(bucket, r.json()["upload_url"], clip)
            vid = r.json()["video_id"]
            r = await client.post(f"/v1/uploads/{vid}/complete", headers=auth, json={})
            assert r.status_code == 200
            assert await run_worker(pool, vid) == "ready"
            return vid

        await upload(draft=False)
        draft_id = await upload(draft=True)  # drafts don't use a slot

        r = await client.post("/v1/uploads", headers=auth, json=body)
        assert r.status_code == 409 and "library is full" in r.json()["detail"]

        r = await client.post(f"/v1/videos/{draft_id}/publish", headers=auth)
        assert r.status_code == 409
    finally:
        await pool.execute("delete from auth.users where email like '%@t.dev'")


async def test_rejections(
    env, bucket: Path, tmp_path: Path, make_token: Callable[..., str]
) -> None:
    client, pool, _ = env
    athlete = await make_user(pool, "athlete")
    coach = await make_user(pool, "coach")
    auth = {"Authorization": f"Bearer {make_token(athlete)}"}
    try:
        base = {"title": "Clip", "size_bytes": 1000, "content_type": "video/mp4"}
        r = await client.post(
            "/v1/uploads", headers={"Authorization": f"Bearer {make_token(coach)}"}, json=base
        )
        assert r.status_code == 403
        r = await client.post(
            "/v1/uploads", headers=auth, json={**base, "content_type": "image/png"}
        )
        assert r.status_code == 415
        r = await client.post("/v1/uploads", headers=auth, json={**base, "size_bytes": 10**10})
        assert r.status_code == 413
        r = await client.post("/v1/uploads", headers=auth, json={**base, "duration_s": 300})
        assert r.status_code == 422

        # a clip that is too short is rejected by the worker with a readable reason
        short = make_clip(tmp_path / "short.mp4", seconds=1, size="640x360", audio=False)
        r = await client.post(
            "/v1/uploads", headers=auth, json={**base, "size_bytes": short.stat().st_size}
        )
        vid = r.json()["video_id"]
        put(bucket, r.json()["upload_url"], short)
        await client.post(f"/v1/uploads/{vid}/complete", headers=auth, json={})
        assert await run_worker(pool, vid) == "rejected"
        row = await pool.fetchrow(
            "select status::text, reject_reason from public.videos where id = $1", vid
        )
        assert row["status"] == "rejected" and "at least 2 seconds" in row["reject_reason"]
    finally:
        await pool.execute("delete from auth.users where email like '%@t.dev'")
