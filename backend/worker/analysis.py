"""arq task: rate a processed clip with pose analysis."""

import json
import logging
import tempfile
from pathlib import Path

import asyncpg
from anyio import to_thread
from arq import Retry

from app.config import get_settings
from app.services import storage
from worker import media
from worker.pose import analyze

log = logging.getLogger("talento.worker.analysis")

MAX_TRIES = 3
FAILED_NOTE = "We couldn't rate this clip, but it's still on your profile."


def _download(bucket: str, key: str, dest: Path) -> None:
    storage.client().download_file(bucket, key, str(dest))


async def _save(pool: asyncpg.Pool, video_id: str, owner_id: str, result: analyze.Analysis) -> bool:
    """Store the analysis and mirror it onto the video. False if the video is gone."""
    async with pool.acquire() as conn, conn.transaction():
        still_there = await conn.fetchval(
            "select 1 from public.videos where id = $1 for update", video_id
        )
        if not still_there:
            return False
        await conn.execute(
            """insert into public.video_analyses
                 (video_id, owner_id, model_version, status, quality, metrics, scores, overall,
                  processing_ms, error)
               values ($1, $2, $3, $4::public.analysis_status, $5::jsonb, $6::jsonb, $7::jsonb,
                       $8, $9, $10)
               on conflict (video_id) do update set
                 model_version = excluded.model_version, status = excluded.status,
                 quality = excluded.quality, metrics = excluded.metrics,
                 scores = excluded.scores, overall = excluded.overall,
                 processing_ms = excluded.processing_ms, error = excluded.error""",
            video_id,
            owner_id,
            result.model_version,
            result.status,
            json.dumps(result.quality),
            json.dumps(result.metrics),
            json.dumps(result.scores),
            result.overall,
            result.processing_ms,
            result.reason if result.status != "done" else None,
        )
        await conn.execute(
            """update public.videos
                  set analysis_status = $2::public.analysis_status, analysis_note = $3, rating = $4
                where id = $1""",
            video_id,
            result.status,
            result.reason,
            result.overall,
        )
    return True


async def analyze_video(ctx: dict, video_id: str) -> str:
    pool: asyncpg.Pool = ctx["db"]
    settings = get_settings()
    row = await pool.fetchrow(
        """select owner_id::text, status::text as status, playback_key, duration_s
             from public.videos where id = $1""",
        video_id,
    )
    if row is None:
        return "gone"
    if row["status"] != "ready" or not row["playback_key"]:
        return f"skipped ({row['status']})"

    try:
        with tempfile.TemporaryDirectory(prefix="talento-pose-") as tmp:
            clip = Path(tmp) / "720.mp4"
            await to_thread.run_sync(_download, settings.r2_bucket_media, row["playback_key"], clip)
            duration = (
                float(row["duration_s"] or 0)
                or (await to_thread.run_sync(media.probe, clip)).duration_s
            )
            result = await to_thread.run_sync(analyze.run, clip, duration)
    except Exception:
        log.exception("analysis of %s failed (try %s)", video_id, ctx.get("job_try"))
        if ctx.get("job_try", 1) < MAX_TRIES:
            raise Retry(defer=ctx.get("job_try", 1) * 30) from None
        result = analyze.Analysis("failed", FAILED_NOTE, {})

    if not await _save(pool, video_id, row["owner_id"], result):
        return "gone"
    log.info(
        "video %s analysis %s overall=%s in %sms",
        video_id,
        result.status,
        result.overall,
        result.processing_ms,
    )
    return result.status
