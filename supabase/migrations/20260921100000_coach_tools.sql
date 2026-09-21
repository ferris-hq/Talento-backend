-- Phase 6: coach tools — shortlists, scouting notes and ratings, athlete search, decisions on
-- applications, recommendations between coaches and trial invites.
--
-- A coach's shortlist, notes and ratings are private to that coach and are read and written
-- straight from the app through RLS. Anything another person ends up seeing (a decision on an
-- application, a recommendation, an invite) goes through a security-definer function that checks
-- who is calling and whether they may see the athlete at all.

-- ---------------------------------------------------------------------------------------------
-- Who counts as a scout
-- ---------------------------------------------------------------------------------------------

-- Verified coaches scout. Admins can do everything a coach can, for support and moderation.
create function private.is_scout() returns boolean
language sql stable security definer set search_path = '' as $$
  select private.is_verified_coach() or private.is_admin()
$$;
revoke execute on function private.is_scout() from public, anon;
grant execute on function private.is_scout() to authenticated, service_role;

-- The policies below call this one directly, so signed-in users need EXECUTE on it.
grant execute on function private.can_view_athlete(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- shortlists, notes, ratings — private to the coach
-- ---------------------------------------------------------------------------------------------

create table public.shortlists (
  coach_id uuid not null references public.profiles (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (coach_id, athlete_id)
);

create index shortlists_coach_idx on public.shortlists (coach_id, created_at desc);

revoke all on public.shortlists from anon, authenticated;
grant select, insert, delete on public.shortlists to authenticated;
grant all on public.shortlists to service_role;
alter table public.shortlists enable row level security;

create policy "coaches read their own shortlist" on public.shortlists
  for select to authenticated using (coach_id = auth.uid());
create policy "scouts shortlist athletes they can see" on public.shortlists
  for insert to authenticated with check (
    coach_id = auth.uid() and private.is_scout() and private.can_view_athlete(athlete_id)
  );
create policy "coaches remove from their own shortlist" on public.shortlists
  for delete to authenticated using (coach_id = auth.uid());

create table public.scout_notes (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.profiles (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  body text not null check (char_length(btrim(body)) between 1 and 1000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index scout_notes_pair_idx on public.scout_notes (coach_id, athlete_id, created_at desc);

create trigger scout_notes_updated_at before update on public.scout_notes
  for each row execute function public.set_updated_at();

revoke all on public.scout_notes from anon, authenticated;
grant select, insert, update, delete on public.scout_notes to authenticated;
grant all on public.scout_notes to service_role;
alter table public.scout_notes enable row level security;

-- Notes are the coach's own record. The athlete never sees them.
create policy "coaches read their own notes" on public.scout_notes
  for select to authenticated using (coach_id = auth.uid());
create policy "scouts write notes on athletes they can see" on public.scout_notes
  for insert to authenticated with check (
    coach_id = auth.uid() and private.is_scout() and private.can_view_athlete(athlete_id)
  );
create policy "coaches edit their own notes" on public.scout_notes
  for update to authenticated using (coach_id = auth.uid()) with check (coach_id = auth.uid());
create policy "coaches delete their own notes" on public.scout_notes
  for delete to authenticated using (coach_id = auth.uid());

create table public.coach_ratings (
  coach_id uuid not null references public.profiles (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  technical smallint not null check (technical between 1 and 5),
  physical smallint not null check (physical between 1 and 5),
  mental smallint not null check (mental between 1 and 5),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (coach_id, athlete_id)
);

create trigger coach_ratings_updated_at before update on public.coach_ratings
  for each row execute function public.set_updated_at();

revoke all on public.coach_ratings from anon, authenticated;
grant select, insert, update, delete on public.coach_ratings to authenticated;
grant all on public.coach_ratings to service_role;
alter table public.coach_ratings enable row level security;

create policy "coaches read their own ratings" on public.coach_ratings
  for select to authenticated using (coach_id = auth.uid());
create policy "scouts rate athletes they can see" on public.coach_ratings
  for insert to authenticated with check (
    coach_id = auth.uid() and private.is_scout() and private.can_view_athlete(athlete_id)
  );
create policy "coaches change their own ratings" on public.coach_ratings
  for update to authenticated using (coach_id = auth.uid()) with check (coach_id = auth.uid());
create policy "coaches delete their own ratings" on public.coach_ratings
  for delete to authenticated using (coach_id = auth.uid());

-- ---------------------------------------------------------------------------------------------
-- recommendations and trial invites — written by functions, read by the people involved
-- ---------------------------------------------------------------------------------------------

create type public.recommendation_status as enum ('sent', 'read', 'dismissed');
create type public.invite_status as enum ('sent', 'applied', 'declined', 'expired');

create table public.recommendations (
  id uuid primary key default gen_random_uuid(),
  from_coach_id uuid not null references public.profiles (id) on delete cascade,
  to_coach_id uuid not null references public.profiles (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  note text check (char_length(note) <= 300),
  suggest_trial boolean not null default false,
  status public.recommendation_status not null default 'sent',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (from_coach_id <> to_coach_id)
);

create index recommendations_inbox_idx on public.recommendations (to_coach_id, created_at desc);
create index recommendations_sent_idx on public.recommendations (from_coach_id, created_at desc);

create trigger recommendations_updated_at before update on public.recommendations
  for each row execute function public.set_updated_at();

revoke all on public.recommendations from anon, authenticated;
grant select on public.recommendations to authenticated;
grant update (status) on public.recommendations to authenticated;
grant all on public.recommendations to service_role;
alter table public.recommendations enable row level security;

-- Only the two coaches see it; the athlete is not told they were passed around.
create policy "recommendations visible to both coaches" on public.recommendations
  for select to authenticated using (
    from_coach_id = auth.uid() or to_coach_id = auth.uid() or private.is_admin()
  );
create policy "recipients mark recommendations read" on public.recommendations
  for update to authenticated using (to_coach_id = auth.uid()) with check (to_coach_id = auth.uid());

create table public.trial_invites (
  id uuid primary key default gen_random_uuid(),
  trial_id uuid not null references public.trials (id) on delete cascade,
  coach_id uuid not null references public.profiles (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  message text check (char_length(message) <= 300),
  status public.invite_status not null default 'sent',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (trial_id, athlete_id)
);

create index trial_invites_athlete_idx on public.trial_invites (athlete_id, created_at desc);

create trigger trial_invites_updated_at before update on public.trial_invites
  for each row execute function public.set_updated_at();

revoke all on public.trial_invites from anon, authenticated;
grant select on public.trial_invites to authenticated;
grant all on public.trial_invites to service_role;
alter table public.trial_invites enable row level security;

create policy "invites visible to the athlete and the inviting coach" on public.trial_invites
  for select to authenticated using (
    athlete_id = auth.uid() or coach_id = auth.uid() or private.owns_trial(trial_id) or private.is_admin()
  );

-- ---------------------------------------------------------------------------------------------
-- Slots and decision notes on applications
-- ---------------------------------------------------------------------------------------------

alter table public.trial_applications
  add column slot text check (char_length(slot) <= 60),
  add column decision_note text check (char_length(decision_note) <= 300);

-- ---------------------------------------------------------------------------------------------
-- Athlete search
-- ---------------------------------------------------------------------------------------------

-- An athlete's headline number: the average AI rating of their most recent published clips,
-- on the same 0-100 scale the app shows.
create function private.athlete_overall(p_athlete uuid, p_clips integer default 5)
returns table (overall integer, clips integer)
language sql stable security definer set search_path = '' as $$
  with recent as (
    select rating from public.videos
     where owner_id = p_athlete and status = 'ready' and not is_draft and rating is not null
     order by coalesce(ready_at, created_at) desc
     limit p_clips
  )
  select round(avg(rating) * 10)::integer, count(*)::integer from recent
$$;
revoke execute on function private.athlete_overall(uuid, integer) from public, anon;

-- How well an athlete fits what this coach is looking for, as a percentage.
--
-- It is deliberately simple and explainable: the sport and region a coach works in, the age
-- groups and positions their open trials ask for, and how the athlete is rated. Everything is
-- known to the coach already, so the number never implies more than it knows.
create function private.match_score(
  p_coach_sport text,
  p_coach_region text,
  p_wanted_age_groups text[],
  p_wanted_positions text[],
  p_sport text,
  p_region text,
  p_age_group text,
  p_position text,
  p_overall integer
) returns integer
language sql immutable set search_path = '' as $$
  select greatest(1, least(99,
    40
    + case when p_coach_sport is null or p_sport is null then 0
           when lower(p_coach_sport) = lower(p_sport) then 20 else -25 end
    + case when p_coach_region is not null and p_region is not null
            and lower(p_coach_region) = lower(p_region) then 12 else 0 end
    + case when cardinality(p_wanted_age_groups) = 0 then 0
           when p_age_group = any (p_wanted_age_groups) then 10 else -5 end
    + case when cardinality(p_wanted_positions) = 0 then 0
           when p_position is not null and lower(p_position) = any (
             select lower(x) from unnest(p_wanted_positions) as x
           ) then 8 else 0 end
    + round(coalesce(p_overall, 45) * 0.2)::integer
  ))
$$;
revoke execute on function private.match_score(text, text, text[], text[], text, text, text, text, integer)
  from public, anon;

-- Athletes a scout may see, ranked. `p_sort` is 'match', 'rating' or 'youngest'.
create function public.search_athletes(
  p_query text default null,
  p_position text default null,
  p_age_group text default null,
  p_region text default null,
  p_sport text default null,
  p_shortlisted boolean default false,
  p_sort text default 'match',
  p_limit integer default 20,
  p_offset integer default 0,
  p_ids uuid[] default null
) returns table (
  id uuid,
  full_name text,
  avatar_key text,
  region text,
  city text,
  sport text,
  "position" text,
  age_group text,
  current_club text,
  overall integer,
  clips integer,
  skills jsonb,
  shortlisted boolean,
  "match" integer
)
language plpgsql stable security definer set search_path = '' as $$
declare
  v_coach_sport text;
  v_coach_region text;
  v_age_groups text[];
  v_positions text[];
  v_query text := nullif(btrim(coalesce(p_query, '')), '');
begin
  if not private.is_scout() then
    raise exception 'Only verified coaches can search athletes.' using errcode = '42501';
  end if;

  select coalesce(c.focus_sport, p.sport), p.region into v_coach_sport, v_coach_region
    from public.profiles p
    left join public.coach_profiles c on c.profile_id = p.id
   where p.id = auth.uid();

  select coalesce(array_agg(distinct t.age_group) filter (where t.age_group is not null), '{}'),
         coalesce(array_agg(distinct t.position) filter (where t.position is not null), '{}')
    into v_age_groups, v_positions
    from public.trials t
   where t.created_by = auth.uid() and t.status = 'open';

  return query
  with athletes as (
    select p.id, p.full_name, p.avatar_key, p.region, p.city, p.sport,
           ap.position, ap.age_group, ap.current_club,
           (select s.overall from private.athlete_overall(p.id) s) as overall,
           (select s.clips from private.athlete_overall(p.id) s) as clips,
           exists (
             select 1 from public.shortlists s
              where s.coach_id = auth.uid() and s.athlete_id = p.id
           ) as shortlisted,
           (select ap2.date_of_birth from public.athlete_private ap2 where ap2.profile_id = p.id)
             as date_of_birth
      from public.profiles p
      join public.athlete_profiles ap on ap.profile_id = p.id
     where p.role = 'athlete'
       and p.id <> auth.uid()
       and (p_ids is null or p.id = any (p_ids))
       and private.can_view_athlete(p.id)
       and (p_position is null or ap.position = p_position)
       and (p_age_group is null or ap.age_group = p_age_group)
       and (p_region is null or p.region = p_region)
       and (p_sport is null or p.sport = p_sport)
       and (v_query is null or p.full_name ilike '%' || v_query || '%'
            or ap.current_club ilike '%' || v_query || '%'
            or p.region ilike '%' || v_query || '%'
            or ap.position ilike '%' || v_query || '%')
  )
  select a.id, a.full_name, a.avatar_key, a.region, a.city, a.sport, a.position, a.age_group,
         a.current_club, a.overall, a.clips,
         coalesce((
           select jsonb_object_agg(s.skill, s.score)
             from (
               select s.skill, s.score from private.current_scores(a.id) s
                order by s.score desc limit 3
             ) s
         ), '{}'::jsonb) as skills,
         a.shortlisted,
         private.match_score(v_coach_sport, v_coach_region, v_age_groups, v_positions,
                             a.sport, a.region, a.age_group, a.position, a.overall) as match
    from athletes a
   where not p_shortlisted or a.shortlisted
   order by
     case when p_sort = 'rating' then a.overall end desc nulls last,
     case when p_sort = 'youngest' then a.date_of_birth end desc nulls last,
     case when p_sort not in ('rating', 'youngest')
          then private.match_score(v_coach_sport, v_coach_region, v_age_groups, v_positions,
                                   a.sport, a.region, a.age_group, a.position, a.overall)
     end desc nulls last,
     a.full_name
   limit least(greatest(coalesce(p_limit, 20), 1), 50)
  offset greatest(coalesce(p_offset, 0), 0);
end;
$$;
revoke execute on function
  public.search_athletes(text, text, text, text, text, boolean, text, integer, integer, uuid[])
  from public, anon;
grant execute on function
  public.search_athletes(text, text, text, text, text, boolean, text, integer, integer, uuid[])
  to authenticated;

-- One athlete, in the same shape as a search result, for their profile page.
create function public.athlete_card(p_athlete uuid)
returns table (
  id uuid,
  full_name text,
  avatar_key text,
  region text,
  city text,
  sport text,
  "position" text,
  age_group text,
  current_club text,
  overall integer,
  clips integer,
  skills jsonb,
  shortlisted boolean,
  "match" integer
)
language sql stable security definer set search_path = '' as $$
  select * from public.search_athletes(p_limit => 1, p_ids => array[p_athlete])
$$;
revoke execute on function public.athlete_card(uuid) from public, anon;
grant execute on function public.athlete_card(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Deciding on applications
-- ---------------------------------------------------------------------------------------------

-- The coach running a trial accepts or rejects an applicant, optionally with a session slot and
-- a short note the athlete sees.
create function public.decide_application(
  p_application uuid,
  p_status text,
  p_slot text default null,
  p_note text default null
) returns public.trial_applications
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_application public.trial_applications;
  v_status public.application_status;
begin
  if p_status not in ('accepted', 'rejected', 'pending') then
    raise exception 'Unknown decision.' using errcode = 'P0001';
  end if;
  v_status := p_status::public.application_status;

  select * into v_application from public.trial_applications where id = p_application for update;
  if not found then
    raise exception 'This application no longer exists.' using errcode = 'P0001';
  end if;
  if not (private.owns_trial(v_application.trial_id) or private.is_admin()) then
    raise exception 'Only the coach running this trial can decide on it.' using errcode = '42501';
  end if;
  if v_application.status = 'withdrawn' then
    raise exception 'This athlete withdrew their application.' using errcode = 'P0001';
  end if;

  update public.trial_applications
     set status = v_status,
         slot = case when v_status = 'accepted' then nullif(btrim(coalesce(p_slot, '')), '') else null end,
         decision_note = nullif(btrim(coalesce(p_note, '')), ''),
         decided_at = case when v_status = 'pending' then null else now() end,
         decided_by = case when v_status = 'pending' then null else auth.uid() end
   where id = p_application
  returning * into v_application;

  return v_application;
end;
$$;
revoke execute on function public.decide_application(uuid, text, text, text) from public, anon;
grant execute on function public.decide_application(uuid, text, text, text) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Recommendations and invites
-- ---------------------------------------------------------------------------------------------

-- Verified coaches a recommendation can be sent to.
create function public.coach_peers(p_query text default null, p_limit integer default 20)
returns table (id uuid, full_name text, avatar_key text, organization text, region text)
language plpgsql stable security definer set search_path = '' as $$
declare
  v_query text := nullif(btrim(coalesce(p_query, '')), '');
begin
  if not private.is_scout() then
    raise exception 'Only verified coaches can do this.' using errcode = '42501';
  end if;
  return query
    select p.id, p.full_name, p.avatar_key, c.organization_text, p.region
      from public.profiles p
      join public.coach_profiles c on c.profile_id = p.id
     where c.verification_status = 'verified'
       and p.id <> auth.uid()
       and (v_query is null or p.full_name ilike '%' || v_query || '%'
            or c.organization_text ilike '%' || v_query || '%')
     order by p.full_name
     limit least(greatest(coalesce(p_limit, 20), 1), 50);
end;
$$;
revoke execute on function public.coach_peers(text, integer) from public, anon;
grant execute on function public.coach_peers(text, integer) to authenticated;

create function public.recommend_athlete(
  p_athlete uuid,
  p_to_coach uuid,
  p_note text default null,
  p_suggest_trial boolean default false
) returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_id uuid;
begin
  if not private.is_scout() then
    raise exception 'Only verified coaches can recommend athletes.' using errcode = '42501';
  end if;
  if p_to_coach = auth.uid() then
    raise exception 'Pick another coach to send this to.' using errcode = 'P0001';
  end if;
  if not private.is_verified_coach(p_to_coach) then
    raise exception 'That coach is not verified yet.' using errcode = 'P0001';
  end if;
  if not private.can_view_athlete(p_athlete) then
    raise exception 'You can''t share this athlete.' using errcode = '42501';
  end if;
  if not exists (select 1 from public.profiles where id = p_athlete and role = 'athlete') then
    raise exception 'That athlete no longer exists.' using errcode = 'P0001';
  end if;

  insert into public.recommendations (from_coach_id, to_coach_id, athlete_id, note, suggest_trial)
  values (auth.uid(), p_to_coach, p_athlete, nullif(btrim(coalesce(p_note, '')), ''),
          coalesce(p_suggest_trial, false))
  returning id into v_id;
  return v_id;
end;
$$;
revoke execute on function public.recommend_athlete(uuid, uuid, text, boolean) from public, anon;
grant execute on function public.recommend_athlete(uuid, uuid, text, boolean) to authenticated;

-- Invite an athlete to a trial the coach runs. The athlete still has to apply themselves, which
-- keeps consent with them; the invite just puts the trial in front of them.
create function public.invite_to_trial(p_trial uuid, p_athlete uuid, p_message text default null)
returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_trial public.trials;
  v_id uuid;
begin
  select * into v_trial from public.trials where id = p_trial;
  if not found then
    raise exception 'This trial no longer exists.' using errcode = 'P0001';
  end if;
  if not (private.owns_trial(p_trial) or private.is_admin()) then
    raise exception 'Only the coach running this trial can invite athletes.' using errcode = '42501';
  end if;
  if v_trial.status <> 'open' or v_trial.application_deadline < current_date then
    raise exception 'Applications for this trial are closed.' using errcode = 'P0001';
  end if;
  if not private.can_view_athlete(p_athlete) then
    raise exception 'You can''t invite this athlete.' using errcode = '42501';
  end if;
  if exists (
    select 1 from public.trial_applications
     where trial_id = p_trial and athlete_id = p_athlete and status <> 'withdrawn'
  ) then
    raise exception 'They have already applied to this trial.' using errcode = 'P0001';
  end if;

  insert into public.trial_invites (trial_id, coach_id, athlete_id, message)
  values (p_trial, auth.uid(), p_athlete, nullif(btrim(coalesce(p_message, '')), ''))
  on conflict (trial_id, athlete_id) do update
     set message = excluded.message, status = 'sent', updated_at = now()
  returning id into v_id;
  return v_id;
end;
$$;
revoke execute on function public.invite_to_trial(uuid, uuid, text) from public, anon;
grant execute on function public.invite_to_trial(uuid, uuid, text) to authenticated;

-- Applying closes any open invite, so a coach can see who took theirs up.
create or replace function public.apply_to_trial(p_trial uuid, p_message text default null)
returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_trial public.trials;
  v_existing public.trial_applications;
  v_block text;
  v_reapply boolean;
  v_id uuid;
begin
  select * into v_trial from public.trials where id = p_trial for update;
  if not found or v_trial.status = 'cancelled' then
    raise exception 'This trial is no longer available.' using errcode = 'P0001';
  end if;

  select * into v_existing from public.trial_applications
   where trial_id = p_trial and athlete_id = auth.uid();
  v_reapply := found;
  if v_reapply and v_existing.status <> 'withdrawn' then
    raise exception 'You have already applied to this trial.' using errcode = 'P0001';
  end if;

  v_block := private.application_block(v_trial, auth.uid());
  if v_block is not null then
    raise exception '%', v_block using errcode = 'P0001';
  end if;

  if v_reapply then
    update public.trial_applications
       set status = 'pending', message = nullif(trim(p_message), ''),
           slot = null, decision_note = null,
           decided_at = null, decided_by = null, created_at = now()
     where id = v_existing.id
    returning id into v_id;
  else
    insert into public.trial_applications (trial_id, athlete_id, message)
    values (p_trial, auth.uid(), nullif(trim(p_message), ''))
    returning id into v_id;
  end if;

  update public.trial_invites
     set status = 'applied'
   where trial_id = p_trial and athlete_id = auth.uid() and status = 'sent';

  return v_id;
end;
$$;

-- Invites waiting for the signed-in athlete, newest first.
create function public.my_trial_invites()
returns table (
  id uuid,
  trial_id uuid,
  message text,
  status public.invite_status,
  created_at timestamptz,
  coach_name text,
  trial_title text,
  club_name text,
  trial_date date,
  application_deadline date,
  trial_status public.trial_status
)
language sql stable security definer set search_path = '' as $$
  select i.id, i.trial_id, i.message, i.status, i.created_at,
         c.full_name, t.title, t.club_name, t.trial_date, t.application_deadline, t.status
    from public.trial_invites i
    join public.trials t on t.id = i.trial_id
    join public.profiles c on c.id = i.coach_id
   where i.athlete_id = auth.uid()
   order by i.created_at desc
   limit 50
$$;
revoke execute on function public.my_trial_invites() from public, anon;
grant execute on function public.my_trial_invites() to authenticated;

create function public.decline_invite(p_invite uuid) returns void
language plpgsql volatile security definer set search_path = '' as $$
begin
  update public.trial_invites
     set status = 'declined'
   where id = p_invite and athlete_id = auth.uid() and status = 'sent';
  if not found then
    raise exception 'This invite can''t be declined.' using errcode = 'P0001';
  end if;
end;
$$;
revoke execute on function public.decline_invite(uuid) from public, anon;
grant execute on function public.decline_invite(uuid) to authenticated;
