"""Enqueueing background jobs (arq over Redis)."""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import HTTPException, status

from app.config import get_settings

PROCESS_VIDEO = "process_video"


async def create_queue() -> ArqRedis:
    settings = RedisSettings.from_dsn(get_settings().redis_url)
    settings.conn_retries = 2
    return await create_pool(settings)


async def enqueue_process_video(queue: ArqRedis | None, video_id: str) -> None:
    if queue is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Processing is unavailable, try again shortly"
        )
    # A fixed job id makes repeated "complete" calls idempotent while the job is queued/running.
    await queue.enqueue_job(PROCESS_VIDEO, video_id, _job_id=f"process:{video_id}")
