"""Postgres access for the service (bypasses RLS: always scope queries to the caller yourself)."""

from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, status


async def create_pool(dsn: str) -> asyncpg.Pool:
    # statement_cache_size=0 keeps us compatible with Supabase's transaction-mode pooler.
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, statement_cache_size=0)


async def get_connection(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool: asyncpg.Pool | None = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database not configured")
    async with pool.acquire() as connection:
        yield connection


DbConnection = Annotated[asyncpg.Connection, Depends(get_connection)]
