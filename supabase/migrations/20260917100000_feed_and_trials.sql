-- Phase 5: trials, applications, likes/saves/views and the home feed.
--
-- Trials are created and edited by verified coaches through the API (which also processes the
-- flyer image). Athletes apply and withdraw through the RPCs below, which enforce eligibility.

-- ---------------------------------------------------------------------------------------------
-- trials
-- ---------------------------------------------------------------------------------------------

create type public.trial_status as enum ('open', 'closed', 'cancelled');
create type public.application_status as enum ('pending', 'accepted', 'rejected', 'withdrawn');

create table public.trials (
  id uuid primary key default gen_random_uuid(),
  created_by uuid not null references public.profiles (id) on delete cascade,
  club_id uuid references public.clubs (id) on delete set null,
  club_name text not null check (char_length(club_name) between 2 and 80),
  title text not null check (char_length(title) between 3 and 80),
  sport text not null,
  position text check (char_length(position) <= 60),
  age_group text check (age_group in ('U-13', 'U-15', 'U-17', 'U-19', 'U-21', 'Senior')),
  skill_level text check (skill_level in ('Beginner', 'Intermediate', 'Advanced')),
  description text not null default '' check (char_length(description) <= 1500),
  requirements text[] not null default '{}' check (cardinality(requirements) <= 10),
  venue text not null check (char_length(venue) between 2 and 120),
  region text,
  trial_date date not null,
  start_time time,
  application_deadline date not null,
  capacity integer check (capacity > 0),
  flyer_key text,
  flyer_width integer,
  flyer_height integer,
  status public.trial_status not null default 'open',
  applicants_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (application_deadline <= trial_date)
);

create index trials_open_idx on public.trials (application_deadline) where status = 'open';
create index trials_created_by_idx on public.trials (created_by, created_at desc);

create trigger trials_updated_at before update on public.trials
  for each row execute function public.set_updated_at();

revoke all on public.trials from anon, authenticated;
grant select on public.trials to authenticated;
grant all on public.trials to service_role;
alter table public.trials enable row level security;

create policy "trials readable unless cancelled" on public.trials
  for select to authenticated using (
    status <> 'cancelled' or created_by = auth.uid() or private.is_admin()
  );

-- ---------------------------------------------------------------------------------------------
-- applications
-- ---------------------------------------------------------------------------------------------

create table public.trial_applications (
  id uuid primary key default gen_random_uuid(),
  trial_id uuid not null references public.trials (id) on delete cascade,
  athlete_id uuid not null references public.profiles (id) on delete cascade,
  status public.application_status not null default 'pending',
  message text check (char_length(message) <= 300),
  decided_at timestamptz,
  decided_by uuid references public.profiles (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (trial_id, athlete_id)
);

create index trial_applications_athlete_idx on public.trial_applications (athlete_id, created_at desc);

create trigger trial_applications_updated_at before update on public.trial_applications
  for each row execute function public.set_updated_at();

revoke all on public.trial_applications from anon, authenticated;
grant select on public.trial_applications to authenticated;
grant all on public.trial_applications to service_role;
alter table public.trial_applications enable row level security;

create function private.owns_trial(p_trial uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.trials where id = p_trial and created_by = auth.uid())
$$;
revoke execute on function private.owns_trial(uuid) from public, anon;
grant execute on function private.owns_trial(uuid) to authenticated;  -- used by RLS

create policy "applications visible to the athlete, the trial owner and admins"
  on public.trial_applications for select to authenticated using (
    athlete_id = auth.uid() or private.owns_trial(trial_id) or private.is_admin()
  );

-- Keep trials.applicants_count equal to the active (not withdrawn) applications.
create function public.sync_trial_applicants() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_trial uuid := coalesce(new.trial_id, old.trial_id);
begin
  update public.trials
     set applicants_count = (
       select count(*) from public.trial_applications
        where trial_id = v_trial and status <> 'withdrawn'
     )
   where id = v_trial;
  return null;
end;
$$;
revoke execute on function public.sync_trial_applicants() from public, anon, authenticated;

create trigger trial_applications_count
  after insert or update of status or delete on public.trial_applications
  for each row execute function public.sync_trial_applicants();

-- Why an athlete can't apply, or null when they can.
create function private.application_block(p_trial public.trials, p_athlete uuid) returns text
language plpgsql stable security definer set search_path = '' as $$
declare
  v_role public.user_role;
  v_sport text;
  v_dob date;
  v_age integer;
  v_limit integer;
begin
  select p.role, p.sport, ap.date_of_birth into v_role, v_sport, v_dob
    from public.profiles p
    left join public.athlete_private ap on ap.profile_id = p.id
   where p.id = p_athlete;

  if v_role is distinct from 'athlete' then
    return 'Only athletes can apply to trials.';
  end if;
  if p_trial.status <> 'open' or p_trial.application_deadline < current_date then
    return 'Applications for this trial are closed.';
  end if;
  if v_sport is distinct from p_trial.sport then
    return 'This trial is for a different sport.';
  end if;
  if p_trial.age_group is not null then
    if v_dob is null then
      return 'Add your date of birth to your profile first.';
    end if;
    v_age := extract(year from age(current_date, v_dob));
    if p_trial.age_group = 'Senior' then
      if v_age < 18 then
        return 'This trial is for senior players (18 and over).';
      end if;
    else
      v_limit := substring(p_trial.age_group from 3)::integer;
      if v_age >= v_limit then
        return format('This trial is for %s players only.', p_trial.age_group);
      end if;
    end if;
  end if;
  if p_trial.capacity is not null and p_trial.applicants_count >= p_trial.capacity then
    return 'This trial is full.';
  end if;
  return null;
end;
$$;
revoke execute on function private.application_block(public.trials, uuid) from public, anon;

create function public.apply_to_trial(p_trial uuid, p_message text default null)
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
           decided_at = null, decided_by = null, created_at = now()
     where id = v_existing.id
    returning id into v_id;
  else
    insert into public.trial_applications (trial_id, athlete_id, message)
    values (p_trial, auth.uid(), nullif(trim(p_message), ''))
    returning id into v_id;
  end if;
  return v_id;
end;
$$;
revoke execute on function public.apply_to_trial(uuid, text) from public, anon;
grant execute on function public.apply_to_trial(uuid, text) to authenticated;

create function public.withdraw_application(p_application uuid) returns void
language plpgsql volatile security definer set search_path = '' as $$
begin
  update public.trial_applications
     set status = 'withdrawn'
   where id = p_application
     and athlete_id = auth.uid()
     and status in ('pending', 'accepted');
  if not found then
    raise exception 'This application can''t be withdrawn.' using errcode = 'P0001';
  end if;
end;
$$;
revoke execute on function public.withdraw_application(uuid) from public, anon;
grant execute on function public.withdraw_application(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- saves, likes and views
-- ---------------------------------------------------------------------------------------------

alter table public.videos add column likes integer not null default 0;

create table public.trial_saves (
  user_id uuid not null references public.profiles (id) on delete cascade,
  trial_id uuid not null references public.trials (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, trial_id)
);

create table public.video_likes (
  user_id uuid not null references public.profiles (id) on delete cascade,
  video_id uuid not null references public.videos (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, video_id)
);

create table public.video_saves (
  user_id uuid not null references public.profiles (id) on delete cascade,
  video_id uuid not null references public.videos (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, video_id)
);

create table public.video_views (
  video_id uuid not null references public.videos (id) on delete cascade,
  viewer_id uuid not null references public.profiles (id) on delete cascade,
  day date not null default current_date,
  primary key (video_id, viewer_id, day)
);

create index video_likes_video_idx on public.video_likes (video_id);
create index video_saves_user_idx on public.video_saves (user_id, created_at desc);

revoke all on public.trial_saves, public.video_likes, public.video_saves, public.video_views
  from anon, authenticated;
grant select, insert, delete on public.trial_saves, public.video_likes, public.video_saves
  to authenticated;
grant all on public.trial_saves, public.video_likes, public.video_saves, public.video_views
  to service_role;

alter table public.trial_saves enable row level security;
alter table public.video_likes enable row level security;
alter table public.video_saves enable row level security;
alter table public.video_views enable row level security;

create policy "own trial saves" on public.trial_saves for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid() and exists (select 1 from public.trials t where t.id = trial_id));

-- The exists() subqueries run under videos' RLS, so people can only like what they can see.
create policy "own video likes" on public.video_likes for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid() and exists (select 1 from public.videos v where v.id = video_id));

create policy "own video saves" on public.video_saves for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid() and exists (select 1 from public.videos v where v.id = video_id));

create function public.sync_video_likes() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_video uuid := coalesce(new.video_id, old.video_id);
begin
  update public.videos
     set likes = (select count(*) from public.video_likes where video_id = v_video)
   where id = v_video;
  return null;
end;
$$;
revoke execute on function public.sync_video_likes() from public, anon, authenticated;

create trigger video_likes_count after insert or delete on public.video_likes
  for each row execute function public.sync_video_likes();

-- One view per person per clip per day; owners watching their own clip don't count.
create function public.record_video_view(p_video uuid) returns void
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_owner uuid;
begin
  select owner_id into v_owner from public.videos
   where id = p_video and status = 'ready' and not is_draft;
  if v_owner is null or v_owner = auth.uid() then
    return;
  end if;
  insert into public.video_views (video_id, viewer_id) values (p_video, auth.uid())
  on conflict do nothing;
  if found then
    update public.videos set views = views + 1 where id = p_video;
  end if;
end;
$$;
revoke execute on function public.record_video_view(uuid) from public, anon;
grant execute on function public.record_video_view(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- feed
-- ---------------------------------------------------------------------------------------------

-- Newest first, keyset-paginated on created_at. p_kind: null (everything), 'highlight', 'trial'.
create function public.get_feed(
  p_kind text default null,
  p_before timestamptz default null,
  p_limit integer default 10
)
returns table (
  kind text,
  id uuid,
  created_at timestamptz,
  title text,
  caption text,
  author_id uuid,
  author_name text,
  author_verified boolean,
  sport text,
  age_group text,
  "position" text,
  region text,
  category text,
  playback_key text,
  poster_key text,
  width integer,
  height integer,
  rating numeric,
  likes integer,
  liked boolean,
  saved boolean,
  flyer_key text,
  venue text,
  trial_date date,
  application_deadline date,
  applicants integer
)
language sql stable security definer set search_path = '' as $$
  with viewer as (
    select auth.uid() as uid,
           private.is_verified_coach() or private.is_admin() as scout
  ),
  clips as (
    select 'highlight'::text, v.id, v.ready_at, v.title, v.caption,
           v.owner_id, p.full_name, false,
           coalesce(v.sport, p.sport), ap.age_group, ap.position, p.region,
           v.category::text, v.playback_key, v.poster_key, v.width, v.height,
           v.rating, v.likes,
           exists (select 1 from public.video_likes l where l.video_id = v.id and l.user_id = viewer.uid),
           exists (select 1 from public.video_saves s where s.video_id = v.id and s.user_id = viewer.uid),
           null::text, null::text, null::date, null::date, null::integer
      from public.videos v
      join public.profiles p on p.id = v.owner_id
      left join public.athlete_profiles ap on ap.profile_id = v.owner_id
      cross join viewer
     where coalesce(p_kind, 'highlight') = 'highlight'
       and v.status = 'ready' and not v.is_draft and v.share_to_feed
       and v.playback_key is not null
       and (v.owner_id = viewer.uid or viewer.scout or private.athlete_is_public(v.owner_id))
       and (p_before is null or v.ready_at < p_before)
     order by v.ready_at desc
     limit least(greatest(p_limit, 1), 30)
  ),
  open_trials as (
    select 'trial'::text, t.id, t.created_at, t.title, t.description,
           t.created_by, t.club_name, true,
           t.sport, t.age_group, t.position, t.region,
           null::text, null::text, null::text, t.flyer_width, t.flyer_height,
           null::numeric, 0,
           false,
           exists (select 1 from public.trial_saves s where s.trial_id = t.id and s.user_id = viewer.uid),
           t.flyer_key, t.venue, t.trial_date, t.application_deadline, t.applicants_count
      from public.trials t
      cross join viewer
     where coalesce(p_kind, 'trial') = 'trial'
       and t.status = 'open' and t.application_deadline >= current_date
       and (p_before is null or t.created_at < p_before)
     order by t.created_at desc
     limit least(greatest(p_limit, 1), 30)
  )
  select * from (select * from clips union all select * from open_trials) feed
   order by 3 desc
   limit least(greatest(p_limit, 1), 30)
$$;
revoke execute on function public.get_feed(text, timestamptz, integer) from public, anon;
grant execute on function public.get_feed(text, timestamptz, integer) to authenticated;

-- Live applicant counts and application status changes in the app.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table public.trial_applications;
  end if;
end
$$;
