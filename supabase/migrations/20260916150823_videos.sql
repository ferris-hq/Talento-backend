-- Videos: uploaded by athletes, processed by the worker (transcode + poster), shown in the feed.
--
-- Lifecycle (status):
--   uploading  -> row created by the API, client is PUTting the raw file to R2
--   processing -> upload confirmed, queued for the worker
--   ready      -> playback_key / poster_key available
--   rejected   -> the clip can't be used (too long, no video stream, ...); reject_reason says why
--   failed     -> processing error after retries
-- Drafts go through the same pipeline (so they have a preview) but stay private until published.
--
-- Clients may read their own videos (and public ready ones) and edit metadata of their own rows.
-- Creating, publishing and deleting go through the API, which also manages the R2 objects.

create type public.video_status as enum ('uploading', 'processing', 'ready', 'rejected', 'failed');
create type public.video_category as enum ('Skills', 'Training', 'Match', 'Passing');

create table public.videos (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles (id) on delete cascade,
  title text not null check (char_length(title) between 1 and 80),
  caption text not null default '' check (char_length(caption) <= 150),
  category public.video_category not null default 'Skills',
  sport text,
  status public.video_status not null default 'uploading',
  reject_reason text,
  is_draft boolean not null default false,
  share_to_feed boolean not null default true,
  raw_key text,
  playback_key text,
  poster_key text,
  content_type text,
  size_bytes bigint check (size_bytes > 0),
  duration_s numeric(7, 2),
  width integer,
  height integer,
  sha256 text,
  rating numeric(3, 1) check (rating between 0 and 10),
  views integer not null default 0,
  processing_started_at timestamptz,
  ready_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index videos_owner_created on public.videos (owner_id, created_at desc);
create index videos_public_feed on public.videos (created_at desc)
  where status = 'ready' and not is_draft and share_to_feed;

create trigger videos_updated_at before update on public.videos
  for each row execute function public.set_updated_at();

-- Library quota: published clips that exist or are on their way (drafts don't count).
create function public.video_slots_used(p_owner uuid) returns integer
language sql stable security definer set search_path = '' as $$
  select count(*)::integer from public.videos
   where owner_id = p_owner
     and not is_draft
     and status in ('uploading', 'processing', 'ready')
$$;
revoke execute on function public.video_slots_used(uuid) from public, anon, authenticated;

revoke all on public.videos from anon, authenticated;
grant select on public.videos to authenticated;
grant update (title, caption, category, share_to_feed) on public.videos to authenticated;
grant all on public.videos to service_role;

alter table public.videos enable row level security;

create policy "owners read their videos" on public.videos
  for select to authenticated using (owner_id = auth.uid());

-- Published, processed clips follow the athlete's visibility (minors without consent: scouts only).
create policy "published videos readable by audience" on public.videos
  for select to authenticated using (
    status = 'ready'
    and not is_draft
    and share_to_feed
    and (private.athlete_is_public(owner_id) or private.is_verified_coach() or private.is_admin())
  );

create policy "owners edit video details" on public.videos
  for update to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Live status updates in the app.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table public.videos;
  end if;
end
$$;
