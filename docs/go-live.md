# Going live

What is already in place, what still needs a decision or a key, and how to check the thing is
healthy once it's out.

---

## What's running now

| Piece | Where | Notes |
|---|---|---|
| Database, auth, realtime | Supabase `Telento` (eu-central-1) | **Free plan** — see the warning below |
| API | Hetzner, `api.talentoafrica.com` | 4 uvicorn workers behind Caddy (TLS, HSTS) |
| Video worker | Same server | ffmpeg + MediaPipe, plus the push sweeper every 20s |
| Storage | Cloudflare R2, `media.talentoafrica.com` | raw / media / private buckets |
| Email | Resend through Supabase SMTP | `noreply@talentoafrica.com`; inbound forwards to the team |
| Admin dashboard | Not hosted yet | Runs locally; Cloudflare Pages or Vercel when wanted |

**Measured capacity**: 50 concurrent callers sustained **325 requests/second** with no failures
and a p95 of about 200 ms (including ~120 ms of network latency from Accra-to-Frankfurt
distance). Re-run any time with `scripts/loadtest.py --users 50 --seconds 45`.

---

## Blocking, before real users

1. **Supabase is on the free plan.** That means no daily backups, and the project is subject to
   free-tier limits and pausing. Moving to Pro ($25/month) is the single most important change
   before anyone's data matters. *Only you can do this — it needs a card.*

2. **There is no admin account.** Nobody can approve coaches, so nobody can post trials. Create
   one, then: `update public.profiles set role = 'admin' where id = '<user id>';`

3. ~~**Supabase Site URL is still `http://localhost:3000`.**~~ Done: it is now
   `https://talentoafrica.com`, and the redirect allow-list already carries `talento://**` and
   `exp://**` for the app's own deep links.

4. **Sentry has no DSN.** `SENTRY_DSN` is empty in `/opt/talento/.env`, so errors are only in
   `docker logs`. Create a project at sentry.io, paste the DSN in, and redeploy. Free tier is
   enough to start.

5. **Leaked-password protection is off.** Supabase can check new passwords against
   HaveIBeenPwned. Turn it on in Authentication → Providers → Email. One toggle.

6. **Privacy policy and terms are published**, at
   https://talento-web-ferris-hqs-projects.vercel.app/privacy and `/terms` (source in the
   `Talento-web` repo). Two things remain: point `talentoafrica.com` at that Vercel project so
   the URLs sit on your own domain, and replace the visible "Before launch" note on both pages
   with the registered company name and address, the Data Protection Commission registration
   number, and who is responsible for data protection.

7. **Register with Ghana's Data Protection Commission** under the Data Protection Act, 2012
   (Act 843). Required to process personal data, and the policy references it.

---

## Worth doing soon

- **Cloudflare purge token.** `CLOUDFLARE_ZONE_ID` and `CLOUDFLARE_API_TOKEN` are empty, so when
  a clip is deleted its file is removed from R2 but may still be served from cache for a while.
  A token with *Zone → Cache Purge* fixes it.
- **Scope the R2 token.** The current token can write to any bucket on the account, including
  another project's. Restrict it to `talento-*`.
- **Uptime monitoring.** Point a monitor (UptimeRobot, Better Stack — free tiers) at:
  - `https://api.talentoafrica.com/healthz` — is the API up
  - `https://api.talentoafrica.com/statusz` — returns **503** when clips are stuck in
    processing or pushes aren't going out, which is how you find out the worker died. A plain
    health check can't see that.
- **Email rate limit.** Supabase sends 30 emails/hour by default; a busy sign-up day will hit
  it. Raise it in Authentication → Rate Limits once on Pro.
- **Host the admin dashboard** at `admin.talentoafrica.com`. The API already allows that origin.

---

## Limits in place

**Per account, in the database** (these cover writes the app makes straight to Supabase, where
an API rate limiter would never see them):

| Action | Limit |
|---|---|
| Applying to trials | 20 per hour |
| Reporting content | 15 per hour |
| Inviting athletes | 60 per hour |
| Recommending athletes | 40 per hour |

**Per account, in the API** (counted in Redis; if Redis is down the request is allowed rather
than blocking an upload):

| Endpoint | Limit |
|---|---|
| `POST /v1/uploads` | 30 per hour |
| `POST /v1/uploads/{id}/complete` | 60 per hour |
| `POST /v1/trials` | 20 per day |
| `DELETE /v1/me` | 5 per hour |

**Other ceilings**: 10 clips per athlete, 90 seconds and 200 MB per clip, 5 MB request bodies at
Caddy, one open report per person per thing.

---

## Checks after a deploy

```bash
curl -s https://api.talentoafrica.com/healthz     # {"status":"ok"}
curl -s https://api.talentoafrica.com/readyz      # database reachable
curl -s https://api.talentoafrica.com/statusz     # queue depth, stuck clips, unsent pushes
ssh talento-api 'docker compose -f /opt/talento/docker-compose.prod.yml ps'
ssh talento-api 'docker logs --tail 50 talento-worker-1'
```

Database and connection notes:

- The service connects through Supabase's **transaction pooler (port 6543)**. Session mode
  (5432) caps us at 15 clients, which the API alone exceeded under load — that's what caused
  18% of readiness checks to fail before it was changed. Migrations and `psql` still use 5432.
- `DB_POOL_SIZE` (default 4) is per process, multiplied by 4 uvicorn workers plus the video
  worker. Raise it only alongside the pooler's client limit.

## If something breaks

| Symptom | Look at |
|---|---|
| Clips stuck "processing" | `/statusz` → `stuck`; then `docker logs talento-worker-1` |
| Nobody gets notifications | `/statusz` → `unsent_pushes`; check the cron lines in the worker log |
| "max clients reached" in logs | Pool size × workers is over the pooler limit; lower `DB_POOL_SIZE` |
| Uploads failing with 429 | The per-account limit is working; raise it in `app/deps/limits.py` if it's wrong |
| A deleted clip still plays | Cloudflare cache — the purge token isn't set (see above) |
