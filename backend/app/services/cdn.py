"""Cloudflare edge cache for media.talentoafrica.com."""

import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


async def purge(urls: list[str | None], settings: Settings) -> None:
    """Best effort: a failed purge is logged, never raised."""
    files = [u for u in urls if u]
    if not files:
        return
    if not (settings.cloudflare_zone_id and settings.cloudflare_api_token):
        log.warning("cloudflare purge not configured; %d deleted files stay cached", len(files))
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.cloudflare.com/client/v4/zones/{settings.cloudflare_zone_id}/purge_cache",
                headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"},
                json={"files": files},
            )
        if r.status_code != 200 or not r.json().get("success"):
            log.error("cloudflare purge failed: %s %s", r.status_code, r.text[:300])
    except httpx.HTTPError:
        log.exception("cloudflare purge failed")
