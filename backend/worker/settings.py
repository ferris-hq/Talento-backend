"""arq worker entrypoint:  arq worker.settings.WorkerSettings"""

import logging
import os

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import create_pool
from worker.analysis import analyze_video
from worker.tasks import process_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def startup(ctx: dict) -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the worker")
    ctx["db"] = await create_pool(settings.database_url)


async def shutdown(ctx: dict) -> None:
    await ctx["db"].close()


class WorkerSettings:
    functions = [process_video, analyze_video]  # noqa: RUF012 - arq reads class attributes
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = int(os.environ.get("WORKER_MAX_JOBS", "1"))
    job_timeout = 20 * 60
    max_tries = 3
    keep_result = 0  # allow re-queuing the same job id right after it finishes
