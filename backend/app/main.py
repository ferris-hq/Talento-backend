import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import create_pool
from app.routers import admin, health, me, trials, videos
from app.services.jobs import create_queue

logger = logging.getLogger("talento")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.db_pool = None
    if settings.database_url:
        app.state.db_pool = await create_pool(settings.database_url, settings.db_pool_size)
    else:
        logger.warning("DATABASE_URL is not set; database-backed endpoints will return 503")
    app.state.queue = None
    try:
        app.state.queue = await create_queue()
    except Exception:
        logger.warning("Redis is unreachable; uploads can't be queued until it is")
    try:
        yield
    finally:
        if app.state.queue is not None:
            await app.state.queue.aclose()
        if app.state.db_pool is not None:
            await app.state.db_pool.close()


def create_app() -> FastAPI:
    settings = get_settings()

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            traces_sample_rate=0.1,
            send_default_pii=False,
        )

    app = FastAPI(
        title="Talento API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(videos.router)
    app.include_router(trials.router)
    app.include_router(admin.router)
    return app


app = create_app()
