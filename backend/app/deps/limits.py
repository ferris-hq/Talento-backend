"""Per-account limits on the endpoints that cost money or reach other people.

Counting lives in Redis, which the worker queue already uses, keyed by account and action.
If Redis is unavailable the request is allowed: a limiter that can't count shouldn't be the
reason an athlete can't upload. The database has its own limits on the writes that go
straight to PostgREST (see the `throttles` migration).
"""

import logging
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.deps.auth import CurrentUser

log = logging.getLogger("talento.limits")

TOO_MANY = "That's a lot in a short time. Try again in a few minutes."


class RateLimit:
    """A dependency that allows `limit` calls per `window_s` seconds, per account."""

    def __init__(self, action: str, limit: int, window_s: int) -> None:
        self.action = action
        self.limit = limit
        self.window_s = window_s

    async def __call__(self, request: Request, user: CurrentUser) -> None:
        redis = getattr(request.app.state, "queue", None)
        if redis is None:
            return

        # A fixed window per account: simple to reason about and cheap to store.
        bucket = int(time.time()) // self.window_s
        key = f"rl:{self.action}:{user.id}:{bucket}"
        try:
            used = await redis.incr(key)
            if used == 1:
                await redis.expire(key, self.window_s + 60)
        except Exception as error:  # never block a request on the limiter itself
            log.warning("rate limit check failed for %s: %s", self.action, error)
            return

        if used > self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, TOO_MANY)


HOUR = 3600
DAY = 24 * HOUR

# Uploads cost storage and a worker slot each, so they are the tightest.
UploadLimit = Annotated[None, Depends(RateLimit("upload", limit=30, window_s=HOUR))]
# Finishing an upload is cheap but shouldn't be a way to queue endless work.
CompleteLimit = Annotated[None, Depends(RateLimit("complete", limit=60, window_s=HOUR))]
# A club posting trials: far more than a real club needs in a day.
TrialLimit = Annotated[None, Depends(RateLimit("trial", limit=20, window_s=DAY))]
# Deleting an account is once in a lifetime; this only stops a stuck retry loop.
DeleteAccountLimit = Annotated[None, Depends(RateLimit("delete_account", limit=5, window_s=HOUR))]
