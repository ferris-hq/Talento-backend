# Talento backend

API, video processing and database for the [Talento app](https://github.com/ferris-hq/Telenty-hub).

| Part | Tech | Where it runs |
|---|---|---|
| Database, auth, realtime | Supabase (Postgres + RLS) | Supabase Cloud, `eu-central-1` |
| API | FastAPI (Python 3.12) | Hetzner, behind Caddy |
| Video + pose workers | arq, ffmpeg, MediaPipe Pose *(phase 3–4)* | Hetzner dedicated vCPU |
| Video files | Cloudflare R2 *(phase 3)* | Cloudflare |

The full roadmap lives in [`docs/PLAN.md`](docs/PLAN.md).

## Layout

```
supabase/
  migrations/        SQL migrations, applied in filename order (Supabase CLI format)
  tests/             SQL assertions for schema + RLS (run against local Postgres or Supabase)
  tests/local/       auth-schema stub so migrations run on plain Postgres
backend/
  app/               FastAPI service
  tests/             pytest
infra/               Docker Compose + Caddy for Hetzner
scripts/             helper scripts (db tests)
.github/workflows/   CI
```

## Local development

### Database tests (plain Postgres, no Docker needed)

```bash
scripts/db-test.sh
```

Creates a throwaway database, loads a stub of Supabase's `auth` schema, applies every migration,
then runs `supabase/tests/*.sql`. Needs `psql` and a local Postgres 15+ (`brew services start postgresql@16`).

### API

```bash
cd backend
uv sync
cp ../.env.example ../.env   # fill in Supabase values
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check .
```

### With the Supabase CLI (optional, needs Docker)

```bash
supabase init           # once, creates supabase/config.toml
supabase start
supabase db reset       # applies supabase/migrations
```

## Deploying

- Migrations: `supabase link --project-ref <ref>` then `supabase db push`.
- API: see [`infra/README.md`](infra/README.md).
