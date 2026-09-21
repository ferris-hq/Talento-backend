"""Postgres access for the service (bypasses RLS: always scope queries to the caller yourself)."""

from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, status


async def create_pool(dsn: str, max_size: int = 5) -> asyncpg.Pool:
    """
    A connection pool for one process.

    Supabase's pooler caps how many clients we may hold, and the API runs several uvicorn
    workers next to the video worker, so each process keeps a small pool rather than a large
    one: `max_size` x workers has to stay under that cap, or requests start failing under
    load with "max clients reached".

    statement_cache_size=0 keeps us compatible with the transaction-mode pooler, which is
    what the API should point at (port 6543); migrations and psql use session mode (5432).
    """
    return await asyncpg.create_pool(dsn, min_size=1, max_size=max_size, statement_cache_size=0)


async def get_connection(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool: asyncpg.Pool | None = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database not configured")
    async with pool.acquire() as connection:
        yield connection


DbConnection = Annotated[asyncpg.Connection, Depends(get_connection)]
