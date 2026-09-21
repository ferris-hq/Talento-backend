import logging

from fastapi import APIRouter, Request, Response, status

router = APIRouter(tags=["health"])

log = logging.getLogger("talento.health")

# Clips that have been "processing" longer than this mean the worker is stuck or gone.
STUCK_MINUTES = 30


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Readiness: dependencies are reachable. Used by the deploy health check."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "not configured"}
    try:
        async with pool.acquire() as connection:
            await connection.fetchval("select 1")
    except Exception:  # any failure means not ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@router.get("/statusz")
async def statusz(request: Request, response: Response) -> dict[str, object]:
    """
    Numbers worth alerting on: how much work is queued, and whether any of it is stuck.

    An uptime monitor can watch this and page on a non-ok status, which catches a dead
    worker — something /healthz can't see, because the API stays perfectly healthy while
    clips pile up unrated.
    """
    pool = getattr(request.app.state, "db_pool", None)
    queue = getattr(request.app.state, "queue", None)
    out: dict[str, object] = {"status": "ok"}

    if pool is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "not configured"}

    try:
        row = await pool.fetchrow(
            """
            select
              (select count(*) from public.videos
                where status in ('uploading', 'processing')) as in_flight,
              (select count(*) from public.videos
                where status = 'processing'
                  and processing_started_at < now() - make_interval(mins => $1)) as stuck,
              (select count(*) from public.videos
                where status = 'failed' and updated_at > now() - interval '1 hour') as failed_1h,
              (select count(*) from public.notifications
                where push_sent_at is null
                  and created_at < now() - interval '10 minutes') as unsent_pushes
            """,
            STUCK_MINUTES,
        )
        out |= dict(row)
    except Exception as error:  # a broken query here shouldn't look like a healthy service
        log.warning("status check failed: %s", error)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}

    if queue is not None:
        try:
            out["queued_jobs"] = await queue.zcard("arq:queue")
        except Exception as error:  # Redis being down is worth reporting, not crashing on
            log.warning("queue depth check failed: %s", error)
            out["queued_jobs"] = None

    if out.get("stuck") or out.get("unsent_pushes"):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        out["status"] = "degraded"
    return out
