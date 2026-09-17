-- Phase 4: pose-based movement analysis.
--
-- The worker writes one row per video into video_analyses and mirrors the outcome onto
-- videos (analysis_status, analysis_note, rating) so the app's existing Realtime
-- subscription on videos picks up new ratings.

create type public.analysis_status as enum ('pending', 'done', 'unrated', 'failed');

alter table public.videos
  add column analysis_status public.analysis_status,
  add column analysis_note text;

create table public.video_analyses (
  video_id uuid primary key references public.videos (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  model_version text not null,
  status public.analysis_status not null,
  quality jsonb not null default '{}',
  metrics jsonb not null default '{}',
  scores jsonb not null default '{}',
  overall numeric(3, 1) check (overall between 0 and 10),
  processing_ms integer,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index video_analyses_owner_idx on public.video_analyses (owner_id, created_at desc)
  where status = 'done';

create trigger video_analyses_updated_at before update on public.video_analyses
  for each row execute function public.set_updated_at();

revoke all on public.video_analyses from anon, authenticated;
grant select on public.video_analyses to authenticated;
grant all on public.video_analyses to service_role;

alter table public.video_analyses enable row level security;

-- Readable exactly when the video itself is readable (the subquery is subject to videos' RLS).
create policy "analyses follow video access" on public.video_analyses
  for select to authenticated using (
    exists (select 1 from public.videos v where v.id = video_analyses.video_id)
  );

-- ---------------------------------------------------------------------------------------------
-- Performance summary for one athlete
-- ---------------------------------------------------------------------------------------------

create function private.can_view_athlete(p_athlete uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select p_athlete = auth.uid()
      or private.athlete_is_public(p_athlete)
      or private.is_verified_coach()
      or private.is_admin()
$$;
revoke execute on function private.can_view_athlete(uuid) from public, anon;

-- An athlete's current skill scores: per-skill average of their latest rated, published clips.
create function private.current_scores(p_athlete uuid, p_clips integer default 5)
returns table (skill text, score numeric, clips integer)
language sql stable security definer set search_path = '' as $$
  with recent as (
    select a.scores
      from public.video_analyses a
      join public.videos v on v.id = a.video_id
     where a.owner_id = p_athlete
       and a.status = 'done'
       and v.status = 'ready'
       and not v.is_draft
     order by a.created_at desc
     limit p_clips
  )
  select s.key, round(avg((s.value)::numeric), 0), (select count(*)::integer from recent)
    from recent, jsonb_each_text(recent.scores) as s(key, value)
   group by s.key
$$;
revoke execute on function private.current_scores(uuid, integer) from public, anon;

create function public.get_athlete_performance(p_athlete uuid default auth.uid())
returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare
  v_sport text;
  v_age_group text;
  v_scores jsonb;
  v_clips integer;
  v_overall numeric;
  v_peer_scores jsonb;
  v_peer_count integer;
  v_better_than numeric;
  v_trend jsonb;
begin
  if p_athlete is null or not private.can_view_athlete(p_athlete) then
    raise exception 'not allowed' using errcode = '42501';
  end if;

  select p.sport, ap.age_group into v_sport, v_age_group
    from public.profiles p
    left join public.athlete_profiles ap on ap.profile_id = p.id
   where p.id = p_athlete;

  select jsonb_object_agg(skill, score), max(clips), round(avg(score) / 10, 1)
    into v_scores, v_clips, v_overall
    from private.current_scores(p_athlete);

  -- Peers: other rated athletes in the same sport and age group.
  with peers as (
    select p.id
      from public.profiles p
      join public.athlete_profiles ap on ap.profile_id = p.id
     where p.id <> p_athlete
       and p.role = 'athlete'
       and p.sport is not distinct from v_sport
       and ap.age_group is not distinct from v_age_group
  ),
  peer_scores as (
    select peers.id, cs.skill, cs.score
      from peers, private.current_scores(peers.id) cs
  ),
  peer_overall as (
    select id, avg(score) / 10 as overall from peer_scores group by id
  )
  select
    (select jsonb_object_agg(skill, round(avg_score, 0))
       from (select skill, avg(score) as avg_score from peer_scores group by skill) s),
    (select count(*) from peer_overall),
    (select avg(case when overall < v_overall then 1.0 else 0.0 end) from peer_overall)
  into v_peer_scores, v_peer_count, v_better_than;

  select coalesce(jsonb_agg(jsonb_build_object('week', week, 'overall', overall) order by week), '[]')
    into v_trend
    from (
      select date_trunc('week', a.created_at)::date as week, round(avg(a.overall), 1) as overall
        from public.video_analyses a
        join public.videos v on v.id = a.video_id
       where a.owner_id = p_athlete
         and a.status = 'done'
         and v.status = 'ready'
         and not v.is_draft
         and a.created_at > now() - interval '26 weeks'
       group by 1
    ) weekly;

  return jsonb_build_object(
    'overall', v_overall,
    'scores', coalesce(v_scores, '{}'),
    'rated_clips', coalesce(v_clips, 0),
    'sport', v_sport,
    'age_group', v_age_group,
    -- Comparisons only mean something with a handful of peers.
    'peer_count', v_peer_count,
    'peer_scores', case when v_peer_count >= 5 then v_peer_scores end,
    'top_percent', case
      when v_peer_count >= 5 and v_overall is not null
        then greatest(1, round((1 - v_better_than) * 100))
    end,
    'trend', v_trend
  );
end;
$$;
revoke execute on function public.get_athlete_performance(uuid) from public, anon;
grant execute on function public.get_athlete_performance(uuid) to authenticated;
