"""arq task: process an uploaded video into a streamable MP4 + poster."""

import hashlib
import logging
import tempfile
from pathlib import Path

import asyncpg
from anyio import to_thread
from arq import Retry

from app.config import get_settings
from app.services import storage
from worker import media

log = logging.getLogger("talento.worker")

MAX_TRIES = 3
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(bucket: str, key: str, dest: Path) -> None:
    storage.client().download_file(bucket, key, str(dest))


def _upload(bucket: str, key: str, src: Path, content_type: str) -> None:
    storage.client().upload_file(
        str(src),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type, "CacheControl": IMMUTABLE_CACHE},
    )


async def _set_failed(pool: asyncpg.Pool, video_id: str, status: str, reason: str) -> None:
    await pool.execute(
        "update public.videos set status = $2::public.video_status, reject_reason = $3"
        " where id = $1",
        video_id,
        status,
        reason,
    )


async def process_video(ctx: dict, video_id: str) -> str:
    pool: asyncpg.Pool = ctx["db"]
    settings = get_settings()
    row = await pool.fetchrow(
        "select id, status::text as status, raw_key from public.videos where id = $1", video_id
    )
    if row is None:
        return "gone"
    if row["status"] != "processing":
        return f"skipped ({row['status']})"

    playback_key, poster_key = storage.media_keys(video_id)
    try:
        with tempfile.TemporaryDirectory(prefix="talento-") as tmp:
            tmpdir = Path(tmp)
            src, out, thumb = tmpdir / "raw", tmpdir / "720.mp4", tmpdir / "poster.jpg"

            await to_thread.run_sync(_download, settings.r2_bucket_raw, row["raw_key"], src)
            info = await to_thread.run_sync(media.probe, src)
            media.validate(info, settings.max_video_seconds)
            sha = await to_thread.run_sync(_sha256, src)

            await to_thread.run_sync(media.transcode, src, out, info)
            await to_thread.run_sync(media.poster, out, thumb, info.duration_s)
            final = await to_thread.run_sync(media.probe, out)

            await to_thread.run_sync(
                _upload, settings.r2_bucket_media, playback_key, out, "video/mp4"
            )
            await to_thread.run_sync(
                _upload, settings.r2_bucket_media, poster_key, thumb, "image/jpeg"
            )
    except media.UnusableVideo as exc:
        log.info("video %s rejected: %s", video_id, exc)
        await _set_failed(pool, video_id, "rejected", str(exc))
        return "rejected"
    except Exception:
        log.exception("video %s processing failed (try %s)", video_id, ctx.get("job_try"))
        if ctx.get("job_try", 1) < MAX_TRIES:
            raise Retry(defer=ctx.get("job_try", 1) * 20) from None
        await _set_failed(
            pool, video_id, "failed", "Something went wrong processing this clip. Try again."
        )
        return "failed"

    width, height = final.display_size
    updated = await pool.fetchval(
        """update public.videos
              set status = 'ready', playback_key = $2, poster_key = $3,
                  duration_s = $4, width = $5, height = $6, sha256 = $7,
                  reject_reason = null, ready_at = now()
            where id = $1 and status = 'processing'
        returning id""",
        video_id,
        playback_key,
        poster_key,
        round(final.duration_s, 2),
        width,
        height,
        sha,
    )
    if updated is None:
        # Deleted (or changed) while we were working: don't leave orphaned files behind.
        await to_thread.run_sync(
            storage.delete_objects, settings.r2_bucket_media, [playback_key, poster_key]
        )
        return "discarded"
    log.info("video %s ready (%.1fs %sx%s)", video_id, final.duration_s, width, height)
    return "ready"
