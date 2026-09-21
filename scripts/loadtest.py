#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Pushes read traffic at the API and reports how long it takes under load.

    scripts/loadtest.py                             # 20 users, 30s, against production
    scripts/loadtest.py --users 50 --seconds 60
    scripts/loadtest.py --base-url http://localhost:8000

It only reads: /healthz, /readyz and /statusz. Signed-in traffic (the feed, athlete search)
goes to Supabase directly, not here, so hammering it from this script would measure
Supabase's rate limits rather than ours. Uploads are deliberately not load-tested against
production: each one would leave a real file in R2 and queue real work.

Reports requests/second and the p50/p95/p99 each endpoint took, so a slow dependency shows
up as a long tail rather than an average that hides it.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import defaultdict

import httpx

PATHS = ["/healthz", "/readyz", "/statusz"]


async def worker(
    client: httpx.AsyncClient,
    deadline: float,
    times: dict[str, list[float]],
    errors: dict[str, int],
) -> None:
    index = 0
    while time.monotonic() < deadline:
        path = PATHS[index % len(PATHS)]
        index += 1
        started = time.monotonic()
        try:
            response = await client.get(path, timeout=20.0)
            elapsed = (time.monotonic() - started) * 1000
            # 503 from /statusz means degraded, not a failed request.
            if response.status_code >= 500 and path != "/statusz":
                errors[f"{path} {response.status_code}"] += 1
            else:
                times[path].append(elapsed)
        except httpx.HTTPError as error:
            errors[f"{path} {type(error).__name__}"] += 1


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://api.talentoafrica.com")
    parser.add_argument("--users", type=int, default=20, help="concurrent callers")
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()

    times: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)

    print(f"==> {args.users} callers for {args.seconds}s against {args.base_url}")
    limits = httpx.Limits(max_connections=args.users * 2, max_keepalive_connections=args.users)
    started = time.monotonic()
    deadline = started + args.seconds
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits) as client:
        await asyncio.gather(
            *(worker(client, deadline, times, errors) for _ in range(args.users))
        )
    ran_for = time.monotonic() - started

    total = sum(len(v) for v in times.values())
    print(f"\n{total} requests in {ran_for:.1f}s — {total / ran_for:.0f} req/s\n")
    print(f"{'path':<12} {'count':>7} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    for path in PATHS:
        values = times[path]
        if not values:
            continue
        print(
            f"{path:<12} {len(values):>7} "
            f"{statistics.median(values):>8.0f}ms {percentile(values, 0.95):>8.0f}ms "
            f"{percentile(values, 0.99):>8.0f}ms {max(values):>8.0f}ms"
        )

    if errors:
        print("\nfailures:")
        for label, count in sorted(errors.items()):
            print(f"  {count:>6}  {label}")
    else:
        print("\nno failed requests")


if __name__ == "__main__":
    asyncio.run(main())
