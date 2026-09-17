"""analyze_video against a real Postgres; R2 and the pose model are faked.

Needs TEST_DATABASE_URL.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.db import create_pool
from app.services import jobs
from tests.test_media import make_clip
from worker import analysis, tasks
from worker.pose import analyze

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="needs TEST_DATABASE_URL")


class FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name: str, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    assert DATABASE_URL
    pool = await create_pool(DATABASE_URL)
    yield pool
    await pool.execute("delete from auth.users where email like '%@analysis.t.dev'")
    await pool.close()


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis, "_download", lambda bucket, key, dest: Path(dest).touch())


async def ready_video(pool: asyncpg.Pool) -> tuple[str, str]:
    user_id, video_id = uuid4(), uuid4()
    await pool.execute(
        "insert into auth.users (id, email) values ($1, $2)", user_id, f"{user_id}@analysis.t.dev"
    )
    await pool.execute(
        """insert into public.videos (id, owner_id, title, status, playback_key, duration_s,
                                      analysis_status)
           values ($1, $2, 'clip', 'ready', 'v/x/720.mp4', 12, 'pending')""",
        video_id,
        user_id,
    )
    return str(user_id), str(video_id)


def fake_run(result: analyze.Analysis):
    def run(clip: Path) -> analyze.Analysis:
        assert clip.exists()
        return result

    return run


async def test_rated_clip_updates_video_and_stores_analysis(pool, monkeypatch) -> None:
    _, video_id = await ready_video(pool)
    scores = {"speed": 70, "agility": 60, "balance": 50, "symmetry": 80, "mobility": 40}
    monkeypatch.setattr(
        analyze,
        "run",
        fake_run(analyze.Analysis("done", None, {"frames": 180}, {"speed": 1.5}, scores, 6.0, 900)),
    )

    assert await analysis.analyze_video({"db": pool, "job_try": 1}, video_id) == "done"

    video = await pool.fetchrow(
        "select analysis_status::text, analysis_note, rating from public.videos where id = $1",
        video_id,
    )
    assert (video["analysis_status"], video["analysis_note"], float(video["rating"])) == (
        "done",
        None,
        6.0,
    )
    row = await pool.fetchrow("select * from public.video_analyses where video_id = $1", video_id)
    assert row["model_version"] == "pose-v1" and row["processing_ms"] == 900
    assert '"speed": 70' in row["scores"]

    # Re-running (e.g. a new model version) replaces the analysis instead of duplicating it.
    monkeypatch.setattr(
        analyze, "run", fake_run(analyze.Analysis("unrated", "Keep your whole body in frame.", {}))
    )
    assert await analysis.analyze_video({"db": pool, "job_try": 1}, video_id) == "unrated"
    video = await pool.fetchrow(
        "select analysis_status::text, analysis_note, rating from public.videos where id = $1",
        video_id,
    )
    assert video["analysis_status"] == "unrated" and video["rating"] is None
    assert "whole body" in video["analysis_note"]
    count = await pool.fetchval(
        "select count(*) from public.video_analyses where video_id = $1", video_id
    )
    assert count == 1


async def test_errors_retry_then_mark_failed(pool, monkeypatch) -> None:
    _, video_id = await ready_video(pool)

    def boom(clip: Path):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(analyze, "run", boom)
    with pytest.raises(analysis.Retry):
        await analysis.analyze_video({"db": pool, "job_try": 1}, video_id)
    assert await analysis.analyze_video({"db": pool, "job_try": 3}, video_id) == "failed"
    status = await pool.fetchval(
        "select analysis_status::text from public.videos where id = $1", video_id
    )
    assert status == "failed"


async def test_skips_videos_that_are_gone_or_not_ready(pool) -> None:
    assert await analysis.analyze_video({"db": pool}, str(uuid4())) == "gone"
    _, video_id = await ready_video(pool)
    await pool.execute("update public.videos set status = 'processing' where id = $1", video_id)
    assert await analysis.analyze_video({"db": pool}, video_id) == "skipped (processing)"


async def test_processing_queues_analysis(pool, monkeypatch, tmp_path) -> None:
    """process_video marks the clip pending and enqueues the rating job."""
    _, video_id = await ready_video(pool)
    await pool.execute(
        "update public.videos set status = 'processing', raw_key = 'raw/x' where id = $1", video_id
    )
    clip = make_clip(tmp_path / "c.mp4", seconds=3, size="640x360", audio=False)
    monkeypatch.setattr(tasks, "_download", lambda b, k, dest: dest.write_bytes(clip.read_bytes()))
    monkeypatch.setattr(tasks, "_upload", lambda *a: None)
    redis = FakeRedis()
    assert (
        await tasks.process_video({"db": pool, "job_try": 1, "redis": redis}, video_id) == "ready"
    )
    assert redis.jobs == [(jobs.ANALYZE_VIDEO, (video_id,), {"_job_id": f"analyze:{video_id}"})]
    status = await pool.fetchval(
        "select analysis_status::text from public.videos where id = $1", video_id
    )
    assert status == "pending"
