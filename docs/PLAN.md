# Talento backend plan — Supabase + FastAPI + MediaPipe Pose + Hetzner

## Context

The Talento mobile app (Expo / React Native, this repo) is fully designed but runs on mock data and simulated behaviour:
- `src/data/mock/*` (feed, trials, coach talents, player, videos) and zustand stores (`src/store/auth.ts`, `videos.ts`, `applications.ts`, `shortlist.ts`) hold all state in memory; nothing survives a reload.
- Sign-in is fake (`useAuthStore.signIn()`), uploads are simulated (`UploadStep` timer in `src/app/video-upload.tsx`), and AI ratings are a random number after 8 s (`useVideoStore.startAnalysis`).
- The feed streams Mixkit stock clips (`src/data/mock/feed.ts`); coach picks, match %, peer comparison and notifications are hard-coded.

Goal: a production backend so athletes can sign up, upload real clips that get an automatic movement analysis, appear in a TikTok-style feed, and apply to trials, while verified coaches scout, shortlist, post trials and recommend players.

**Decisions already made**
- Supabase Cloud for Postgres, Auth and Realtime.
- Cloudflare R2 for video files.
- FastAPI and MediaPipe workers on Hetzner.
- AI scores sport-agnostic movement at launch.
- Anyone can register as a coach, but posting trials and inviting athletes needs admin-approved verification.

---

## 1. Architecture

```
Expo app ──supabase-js──► Supabase Cloud (Auth, Postgres + RLS, Realtime)
   │                              ▲
   │ HTTPS (Supabase JWT)         │ service-role (server only)
   ▼                              │
Hetzner: Caddy ─► FastAPI (api) ──┤
                     │ enqueue    │
                     ▼            │
                  Redis ─► arq worker(s): ffmpeg + MediaPipe Pose ─┘
                                   │
   app ◄── CDN (media.talento…) ◄── Cloudflare R2 (raw/ private, media/ public)
```

**Split of responsibilities (rule of thumb)**
- **App → Supabase directly (supabase-js + RLS):** simple reads, and writes that don't need validation or side effects. Examples: profile edits, likes/saves, shortlist, scout notes, reading notifications, withdrawing an application.
- **App → FastAPI:** anything privileged, validated or expensive:
  - upload signing, video processing and scoring
  - feed ranking and coach AI picks
  - posting trials, applying to trials, recommendations and invites
  - coach verification and admin work
  - push notification fan-out
- **Workers:** all video work (transcode, poster, pose, metrics). Never inside a request.

**Regions:** Supabase `eu-central-1` (Frankfurt) and Hetzner Nuremberg/Falkenstein keep API↔DB latency low. R2 serves video from Cloudflare's edge, including Accra, so playback is fast for users in Ghana.

---

## 2. Repo layout (separate backend repo)

The Expo app keeps its own repo (`ferris-hq/Telenty-hub`). This repo (`Talento-backend`) holds everything server-side:

```
supabase/             config.toml, migrations/*.sql, seed.sql, tests/*.sql
backend/
  pyproject.toml      (uv) fastapi, pydantic-settings, supabase/postgrest or asyncpg,
                      boto3 (R2), arq, redis, mediapipe, opencv-python-headless, numpy, scipy,
                      exponent-server-sdk, sentry-sdk, httpx, pyjwt
  app/                FastAPI: main.py, deps/auth.py, routers/, services/, schemas/
  worker/             arq settings, tasks/video.py, pose/{landmarker,metrics,quality,scoring}.py
  models/             pose_landmarker_full.task (downloaded at build)
  tests/              pytest + golden videos
infra/
  docker-compose.prod.yml, Caddyfile, .env.example
.github/workflows/    backend-ci.yml, deploy.yml, supabase-migrations.yml
```

---

## 3. Data model (Supabase Postgres)

Every table has `id uuid pk default gen_random_uuid()`, `created_at` and `updated_at`, and RLS enabled. Enums are Postgres enums.

**Identity and people**
- `profiles`: `id = auth.users.id`, `role (athlete|coach|admin)`, `full_name`, `avatar_key`, `region`, `city`, `sport`, `onboarded_at`.
- `athlete_profiles`:
  - `profile_id`, `date_of_birth`, `age_group` (generated from DOB), `position`, `preferred_side`, `height_cm`, `weight_kg`, `current_club`
  - guardian fields: `guardian_name`, `guardian_phone`, `guardian_consent_at`
  - `visibility (public|scouts_only)`
- `clubs`: `name`, `badge_key`, `region`, `verified_at`.
- `coach_profiles`: `profile_id`, `club_id`, `organization_text`, `focus_sport`, `verification_status (unverified|pending|verified|rejected)`, `verified_at`, `verified_by`.
- `coach_verification_requests`: `coach_id`, `documents` (R2 keys), `notes`, `status`, `reviewed_by`, `reviewed_at`.

**Video and AI**
- `videos`:
  - `owner_id`, `title`, `caption`, `category`, `sport`
  - `status (draft|uploading|processing|ready|rejected|failed)`, `reject_reason`
  - `raw_key`, `playback_key`, `poster_key`, `duration_s`, `width`, `height`, `sha256`, `phash`
  - `share_to_feed`, `kind (highlight|trial)`, `trial_id` nullable
  - `rating numeric(3,1)`, `views`, `likes_count`, `saves_count`, `shares_count`
- `video_analyses`: `video_id`, `model_version`, `status`, `quality jsonb` (person ratio, visibility, full-body %, lighting, fps), `metrics jsonb` (raw values), `scores jsonb` (0–100 per skill), `overall numeric`, `landmarks_key` (R2 parquet), `processing_ms`, `error`.
- `athlete_skill_snapshots`: `athlete_id`, `period_start` (weekly), `scores jsonb`, `overall`. This feeds the Performance progress chart.
- `peer_benchmarks` (materialized view, refreshed nightly): percentiles per `sport × age_group × position × skill`.

**Feed and engagement**
- `video_likes`, `video_saves`, `video_shares` (`user_id`, `video_id`, unique pair). Counters are maintained by triggers.
- `video_views` (`user_id` nullable, `video_id`, `watched_ms`, `completed`), written in batches through the API.
- `reports`: `reporter_id`, `target_type (video|profile|trial)`, `target_id`, `reason`, `status`.
- `blocks`: `blocker_id`, `blocked_id`.

**Trials**
- `trials`:
  - `club_id`, `created_by` (a verified coach), `sport`, `role`, `age_group`, `skill_level`
  - `venue_name`, `lat`, `lng`, `trial_date`, `start_time`, `application_deadline`
  - `description`, `requirements text[]`, `capacity`, `status (draft|open|closed|completed)`, `promo_video_id` (club feed posts)
- `trial_applications`: `trial_id`, `athlete_id`, `status (pending|accepted|rejected|withdrawn|attended|no_show)`, `session_slot`, `message`, `decided_by`, `decided_at`. Unique per (trial, athlete).

**Coach tools**
- `shortlists`: `coach_id`, `athlete_id`, unique pair.
- `scout_notes`: `coach_id`, `athlete_id`, `body`.
- `coach_ratings`: `coach_id`, `athlete_id`, `technical`, `physical`, `mental` (1–5), unique pair.
- `recommendations`: `from_coach_id`, `to_coach_id`, `athlete_id`, `note`, `suggest_trial`, `status`.
- `trial_invites`: `trial_id`, `coach_id`, `athlete_id`, `status`.

**Notifications**
- `notifications`: `user_id`, `type`, `title`, `body`, `data jsonb` (deep link), `read_at`.
- `push_tokens`: `user_id`, `expo_token`, `platform`, `last_seen`.

**RLS essentials**
- Athletes read and write only their own profile rows, videos and applications.
- Public read on `ready` videos with `share_to_feed = true`, excluding blocked users.
- Coaches read athlete profiles unless visibility is `scouts_only`, which verified coaches can still see.
- Only verified coaches can insert trials. This is enforced by the API and backed by an RLS check on `coach_profiles.verification_status`.
- A coach's shortlist, notes and ratings are private to that coach.
- Club-level sharing is a later option.
- No direct client inserts into `video_analyses`, `trials`, `recommendations` or `notifications`. The service role writes those.
- An athlete's contact details are never exposed to coaches. Contact happens through invites.

**Postgres functions (RPC) used by the app**
- `get_feed(p_filter, p_cursor, p_limit)`: the ranked feed.
- `get_athlete_performance(p_athlete)`: scores, peer percentiles and trend.
- `search_athletes(...)`: backs the coach Players tab.

---

## 4. FastAPI service (`backend/app`)

- **Auth:** verify the Supabase JWT on each request against the project JWKS (`deps/auth.py`) and load `profiles.role`. Use the service-role key only on the server.
- **Endpoints (`/v1`)**

| Area | Endpoint | Purpose |
|---|---|---|
| Uploads | `POST /uploads` | Validate quota (`MAX_VIDEOS`) and duration claim; create a `videos` row with status `uploading`; return a presigned R2 PUT URL (multipart if over 50 MB) for `raw/{user}/{video}.mp4` |
| | `POST /uploads/{id}/complete` | HEAD-check the object, set status `processing`, enqueue `process_video` |
| | `POST /videos/{id}/draft` | Save metadata without processing |
| Videos | `GET /videos/{id}` | Status, rating, scores (the app polls this or uses Realtime) |
| | `POST /videos/{id}/views` | Batched watch events |
| Feed | `GET /feed?filter=for_you\|trials\|highlights\|clubs&cursor=` | Calls `get_feed`, adds signed or CDN URLs |
| Performance | `GET /athletes/{id}/performance` | Radar, breakdown vs peers, strongest/focus skill, trend |
| Trials | `POST /trials` (verified coach), `PATCH /trials/{id}`, `POST /trials/{id}/apply` (athlete; checks deadline, age group, duplicates), `GET /trials/{id}/applications`, `PATCH /applications/{id}` (accept/reject/slot) | |
| Coach | `GET /coach/picks` | Match score = f(sport, position, age group, region, skill fit, recency) |
| | `POST /recommendations`, `POST /trial-invites` | |
| | `POST /coach/verification` | Upload documents via presigned URL |
| Admin | `GET/PATCH /admin/verifications`, `/admin/reports`, `/admin/videos/{id}/reject` | Protected by `role=admin` |
| Account | `POST /push-tokens`, `DELETE /me` | Account and data deletion |

- **Cross-cutting**
  - Pydantic schemas mirror `src/types/index.ts`.
  - Rate limiting (slowapi + Redis).
  - Sentry.
  - Structured JSON logs.
  - `/healthz` and `/readyz` endpoints.
  - OpenAPI output generates TS types for the app (`openapi-typescript`).

---

## 5. Video pipeline and MediaPipe Pose (`backend/worker`)

**`process_video(video_id)` (arq task, idempotent, retried up to 3 times)**

1. **Download** `raw_key` from R2 to a temp dir.
2. **Probe** with `ffprobe`:
   - reject if shorter than 5 s or longer than 90 s (`MAX_SECONDS` in `video-upload.tsx`)
   - reject on unsupported codec
   - record fps, width, height and rotation
3. **Integrity checks:**
   - `sha256` exact-duplicate check
   - perceptual hash (sampled frames) near-duplicate check against the same owner and others
   - flag re-uploads of someone else's clip
4. **Transcode** with ffmpeg:
   - 720p portrait H.264 MP4 with faststart and AAC, bitrate capped around 2.5 Mbps, to `media/v/{id}/720.mp4`
   - poster JPG at 0.5 s to `media/v/{id}/poster.jpg`
   - HLS can come later if bandwidth requires it
5. **Pose analysis:**
   - MediaPipe Tasks `PoseLandmarker` (`pose_landmarker_full.task`), `RunningMode.VIDEO`, `num_poses=2`
   - decode frames with OpenCV and sample to 15 fps (30 fps for clips under 20 s)
   - keep the dominant person per frame (largest bbox, tracked by IoU) and flag if multiple people are dominant
   - save the landmarks series (normalized and world) as parquet to `analysis/{id}/landmarks.parquet` for re-scoring without re-running pose
6. **Quality gate (`pose/quality.py`)**. It feeds the "Clip needs a re-upload" notification that already exists in `notifications.tsx`:

| Check | Threshold |
|---|---|
| Person detected | at least 70% of frames |
| Mean landmark visibility | at least 0.6 |
| Full body (ankles and shoulders visible) | at least 60% |
| Brightness | above minimum |
| Motion present | yes |

   - On failure: status `rejected` with a readable reason ("too dark", "keep full body in frame").
7. **Metrics (`pose/metrics.py`)**:
   - Smooth landmarks (Savitzky–Golay filter).
   - Normalize by torso length so distance from camera doesn't matter.
   - Compute per window, then aggregate.

| Skill (0–100) | Signal |
|---|---|
| Speed | Peak and 90th-percentile hip-centre velocity (body lengths per second) |
| Agility | Direction-change rate and deceleration peaks of hip centre |
| Explosiveness | Peak vertical acceleration and jump height estimate (hip displacement) |
| Balance | Centre-of-mass sway relative to the base of support during stable phases |
| Coordination | Movement smoothness (normalized jerk) and limb timing correlation |
| Symmetry | Left/right joint-angle correlation (knee, hip, elbow, shoulder) |
| Range of motion | Knee, hip and shoulder angle spans |
| Work rate | Share of time active above a movement threshold |

8. **Scoring (`pose/scoring.py`):**
   - Raw metric → percentile against `peer_benchmarks` (sport × age group). Use a seeded prior distribution until enough data exists.
   - Overall rating 0–10 = weighted mean of skill scores.
   - Store `model_version` so benchmarks can be re-run later.
9. **Write results:**
   - `video_analyses` and `videos.rating`, `playback_key`, `poster_key`, status `ready`
   - upsert this week's `athlete_skill_snapshots`
   - insert a notification and send an Expo push
10. Delete the raw file after 7 days (R2 lifecycle rule), unless a report is open.

**Honesty in the product:** pose tracking measures movement, not ball skill. So:
- The app's football-only labels (Shooting, Passing, Dribbling…) are replaced by the generic skills above.
- "AI verified" means the clip passed the quality and integrity checks, not that the talent is certified.

**Throughput:** CPU only. Budget roughly 1–2 s of compute per second of video on a dedicated vCPU, so a 60 s clip takes about 1–2 min. arq runs `max_jobs = vCPUs / 2`. Add worker servers horizontally when the queue lags.

---

## 6. Cloudflare R2

- **Buckets:**
  - `talento-raw`: private; direct presigned PUT only; 7-day lifecycle.
  - `talento-media`: public through a custom domain (`media.<domain>`) with Cloudflare cache.
  - `talento-private`: verification documents and landmarks; presigned GET only.
- CORS is only needed for web uploads.
- The server holds the S3 credentials (boto3 with the R2 endpoint). The app never receives keys.
- Posters and playback use immutable CDN URLs with long cache.
- Avatars and club badges also go to `talento-media` via presigned upload.

---

## 7. Hetzner deployment

**Servers (Nuremberg, private network)**

| Role | Server | Runs |
|---|---|---|
| `api-1` | Shared vCPU, 4 vCPU / 8 GB | Caddy (TLS), FastAPI (uvicorn, 4 workers), Redis |
| `worker-1` | Dedicated vCPU, 4–8 vCPU | arq worker containers with ffmpeg and MediaPipe |

- Scale by adding worker servers pointed at the same Redis over the private network.
- **Firewall:** 80/443 to `api-1` only; SSH restricted to your IP/keys; Redis bound to the private network.
- **Delivery:**
  - Docker images built in GitHub Actions and pushed to GHCR.
  - `deploy.yml` SSHes in and runs `docker compose pull && up -d` with health checks.
  - Secrets live in GitHub Environments, rendered to `/opt/talento/.env`.
- **Supabase migrations:** `supabase db push` in CI on merge to `main`, gated on pgTAP tests. Use a staging project first.
- **Ops:**
  - Sentry (API, worker, Expo)
  - uptime monitor on `/healthz`
  - arq queue-depth metric with an alert
  - Hetzner snapshot of the servers weekly (stateless apart from Redis)
  - Supabase daily backups (Pro plan)
- **Environments:** `staging` (a small Supabase project, one combined server) and `production`.

**Rough monthly cost at launch**

| Item | Approx. cost |
|---|---|
| Supabase Pro | $25 |
| Hetzner API server | €15 |
| Hetzner dedicated worker | €30–60 |
| R2 | about $0.015/GB-month, no egress fees |
| Sentry and uptime | free tiers |

---

## 8. Mobile app integration

**Add:**
- `@supabase/supabase-js`
- `expo-secure-store`, for session persistence
- `@tanstack/react-query`, for server state; zustand stays for UI state
- `expo-notifications`
- `expo-file-system` `createUploadTask`, for real upload progress
- `expo-auth-session` / native Google and Apple sign-in, which needs a development build (not Expo Go)

**Config:** `EXPO_PUBLIC_SUPABASE_URL`, `EXPO_PUBLIC_SUPABASE_ANON_KEY`, `EXPO_PUBLIC_API_URL` in `app.config` / `.env`.

**Files to change**

| File | Change |
|---|---|
| `src/lib/supabase.ts`, `src/lib/api.ts` (new) | Clients; the API attaches the Supabase access token |
| `src/store/auth.ts` | Real session, role and profile from Supabase; keep `nextRoute()` and `isOnboarded()` logic |
| `src/app/(auth)/*` | Login, register, profile setup and coach setup write to `profiles`, `athlete_profiles`, `coach_profiles`; coach setup starts verification |
| `src/store/videos.ts`, `src/app/video-upload.tsx` | Replace the simulated `UploadStep` and `startAnalysis` with `POST /uploads` → PUT to R2 with progress → `/complete` → Realtime subscription on the `videos` row; drafts become `videos.status='draft'` |
| `src/components/feed/VideoFeed.tsx`, `src/app/(tabs)/index.tsx`, `src/app/(coach)/index.tsx` | `useInfiniteQuery(GET /feed)`; likes, saves and shortlist become mutations; view events batched |
| `src/app/(tabs)/trials.tsx`, `src/app/trial/[id].tsx`, `src/store/applications.ts` | Trials list/detail and apply via API; "My applications" from `trial_applications` |
| `src/app/performance.tsx`, `src/components/player/SkillRadar.tsx` | Data from `/athletes/{id}/performance`; generic skill labels |
| `src/app/(coach)/*`, `src/app/talent/[id].tsx`, `src/app/recommend-player.tsx`, `src/store/shortlist.ts` | Players search, picks, shortlist, notes, ratings, recommendations, invites; posting trials gated on `verification_status` |
| `src/app/notifications.tsx` | `notifications` table + Realtime + push |
| `src/types/index.ts` | Regenerate from `supabase gen types` and the OpenAPI types |

- Delete `src/data/mock/*` once each screen is wired (keep `seed.sql` for demos).

---

## 9. Safety, privacy and compliance (many athletes are minors)

- Collect date of birth. Under-18s need guardian name, phone and a consent checkbox before their videos go public; until then they're visible to verified coaches only.
- Coaches can't message athletes freely. Contact goes through trial invites and applications only, with in-app records.
- Report and block on videos, profiles and trials, with an admin moderation queue. Suspicious trials can be auto-hidden after N reports.
- Show only the athlete's region/city publicly. Trial venue is visible only to accepted athletes if the club chooses.
- Account deletion (`DELETE /me`) removes R2 objects and rows, as required by app store policy.
- Follow Ghana's Data Protection Act, 2012 (Act 843): privacy policy, consent logging, and registration with the Data Protection Commission.
- Keep the Mixkit placeholder clips out of production. Real uploads only.

---

## 10. Build phases (in order)

1. **Foundations:** Supabase staging project, `supabase/` migrations for identity tables and RLS, `backend/` skeleton with JWT auth and `/healthz`, R2 buckets, Hetzner staging server, CI pipelines.
2. **Auth and onboarding:** email/phone OTP first, then Google/Apple in a development build. Profiles for both roles, guardian consent, coach verification requests plus a minimal admin screen (Supabase Studio works initially).
3. **Upload pipeline without AI:** presigned upload, complete, transcode, poster, `ready`. The Videos tab and drafts wired to real data.
4. **Pose analysis:** landmarker, quality gate, metrics, seeded benchmarks, scoring, Realtime status, push notification. The Performance screen wired. Build the golden-video test set here.
5. **Feed and trials:** `get_feed` ranking v1 (recency decay × engagement × rating × sport/region match, excluding blocked users), likes/saves/views, trials list/detail/apply, My applications.
6. **Coach side:** verified-coach trial posting, application review (accept/reject/slot), search, shortlist, notes, ratings, recommendations, invites, AI picks match score, coach Discover feed.
7. **Notifications and safety:** notification types for every event already in the UI, push tokens, reports/blocks, moderation queue, account deletion.
8. **Production hardening:** production Supabase and Hetzner, rate limits, Sentry, alerts, load test (50 concurrent uploads), privacy policy and store listings, closed beta with one or two academies.

---

## 11. Verification

- **Database:** `supabase start` locally; pgTAP tests in `supabase/tests` assert RLS per role. For example:
  - an athlete can't read another athlete's drafts
  - an unverified coach can't insert trials
  - notes are coach-private
- **API:** pytest with httpx against local Supabase and MinIO (an R2 stand-in), using JWTs minted for athlete, coach, verified coach and admin. Contract tests compare the OpenAPI output to generated app types in CI.
- **Worker:** a golden set of about 20 labelled clips (good, too dark, partial body, two people, static, duplicate) with expected quality outcomes and score ranges. Run on every change to `pose/`; fail on regressions. Check `processing_ms` stays within budget.
- **End to end on a phone** (development build on your Android phone and the iOS simulator):
  - sign up an athlete → upload a 45 s clip → watch the status go processing → ready with scores → appears in the feed
  - coach signs up → verification approved in admin → posts a trial → athlete applies → coach accepts → athlete gets a push notification and sees "Accepted"
- **Staging deploy:** GitHub Actions deploys to Hetzner staging; smoke test `/healthz`, one upload, one feed page; check Sentry has no new errors before promoting to production.
