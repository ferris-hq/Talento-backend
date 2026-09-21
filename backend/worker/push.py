"""arq cron job: push the notifications the database has written but nobody has been told about.

Notifications are created inside the database, next to the change that caused them, so nothing
there can call out to Expo. This job picks up rows that haven't been pushed yet and sends one
Expo request per batch. Tokens Expo says are dead are removed, so we stop pushing to them.
"""

import json
import logging
from typing import Any

import asyncpg
import httpx

log = logging.getLogger("talento.worker.push")

EXPO_URL = "https://exp.host/--/api/v2/push/send"
# Expo accepts up to 100 messages per request.
BATCH = 100
# Anything older than this is stale news; mark it sent rather than pushing it late.
MAX_AGE_HOURS = 2
TIMEOUT = 15.0

PENDING = """
select n.id, n.title, n.body, n.data, n.created_at,
       coalesce(array_agg(t.token) filter (where t.token is not null), '{}') as tokens
  from public.notifications n
  left join public.push_tokens t on t.user_id = n.user_id
 where n.push_sent_at is null
   and n.created_at > now() - make_interval(hours => $1)
 group by n.id
 order by n.created_at
 limit $2
"""


def _message(token: str, row: asyncpg.Record) -> dict[str, Any]:
    data = row["data"]
    return {
        "to": token,
        "title": row["title"],
        "body": row["body"],
        "data": json.loads(data) if isinstance(data, str) else dict(data or {}),
        "sound": "default",
        "channelId": "default",
    }


async def _post(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            EXPO_URL,
            json=messages,
            headers={"accept": "application/json", "content-type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
    tickets = body.get("data")
    return tickets if isinstance(tickets, list) else []


async def send_pending_pushes(ctx: dict) -> int:
    """Sends one batch. Returns how many notifications were dealt with."""
    pool: asyncpg.Pool = ctx["db"]
    rows = await pool.fetch(PENDING, MAX_AGE_HOURS, BATCH)
    if not rows:
        return 0

    messages: list[dict[str, Any]] = []
    for row in rows:
        messages += [_message(token, row) for token in row["tokens"]]

    if messages:
        try:
            tickets = await _post(messages)
        except (httpx.HTTPError, ValueError) as error:
            # Leave the rows unsent; the next run tries again until they age out.
            log.warning("expo push failed: %s", error)
            return 0

        dead = [
            messages[i]["to"]
            for i, ticket in enumerate(tickets)
            if i < len(messages)
            and ticket.get("status") == "error"
            and (ticket.get("details") or {}).get("error") == "DeviceNotRegistered"
        ]
        if dead:
            await pool.execute("delete from public.push_tokens where token = any($1::text[])", dead)
            log.info("removed %d dead push tokens", len(dead))

    # Mark them sent either way: with no device registered there is nothing more to do.
    await pool.execute(
        "update public.notifications set push_sent_at = now() where id = any($1::uuid[])",
        [row["id"] for row in rows],
    )
    log.info("pushed %d notifications to %d devices", len(rows), len(messages))
    return len(rows)
