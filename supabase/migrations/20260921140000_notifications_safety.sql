-- Phase 7: notifications, push tokens, reporting, blocking and account deletion.
--
-- Notifications are written by the database itself, next to the change that caused them, so a
-- decision and the notification about it can never come apart. Nobody can write a notification
-- to someone else: clients may only read their own and mark them read.
--
-- Blocking is mutual and quiet: neither person sees the other's clips, and neither is told.

-- ---------------------------------------------------------------------------------------------
-- notifications
-- ---------------------------------------------------------------------------------------------

create type public.notification_type as enum (
  'video_ready',
  'video_rejected',
  'application_received',
  'application_accepted',
  'application_rejected',
  'trial_invite',
  'trial_cancelled',
  'recommendation',
  'coach_verified',
  'coach_rejected'
);

create table public.notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  type public.notification_type not null,
  title text not null check (char_length(title) between 1 and 120),
  body text not null default '' check (char_length(body) <= 400),
  -- where tapping it should go, e.g. {"href": "/trial/123"}
  data jsonb not null default '{}',
  read_at timestamptz,
  -- set once the push has gone out, so the worker doesn't send it twice
  push_sent_at timestamptz,
  created_at timestamptz not null default now()
);

create index notifications_inbox_idx on public.notifications (user_id, created_at desc);
create index notifications_unsent_idx on public.notifications (created_at) where push_sent_at is null;

revoke all on public.notifications from anon, authenticated;
grant select on public.notifications to authenticated;
grant update (read_at) on public.notifications to authenticated;
grant all on public.notifications to service_role;
alter table public.notifications enable row level security;

create policy "people read their own notifications" on public.notifications
  for select to authenticated using (user_id = auth.uid());
create policy "people mark their own notifications read" on public.notifications
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

-- The one way a notification is created. Called by the triggers below, never by a client.
create function private.notify(
  p_user uuid,
  p_type public.notification_type,
  p_title text,
  p_body text default '',
  p_data jsonb default '{}'
) returns void
language plpgsql volatile security definer set search_path = '' as $$
begin
  if p_user is null then
    return;
  end if;
  insert into public.notifications (user_id, type, title, body, data)
  values (p_user, p_type, left(p_title, 120), left(coalesce(p_body, ''), 400), coalesce(p_data, '{}'));
end;
$$;
revoke execute on function private.notify(uuid, public.notification_type, text, text, jsonb)
  from public, anon, authenticated;

create function public.mark_notifications_read(p_ids uuid[] default null) returns integer
language sql volatile security definer set search_path = '' as $$
  with updated as (
    update public.notifications
       set read_at = now()
     where user_id = auth.uid()
       and read_at is null
       and (p_ids is null or id = any (p_ids))
    returning 1
  )
  select count(*)::integer from updated
$$;
revoke execute on function public.mark_notifications_read(uuid[]) from public, anon;
grant execute on function public.mark_notifications_read(uuid[]) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Notifications for the things that already happen
-- ---------------------------------------------------------------------------------------------

-- A clip finishes processing, or can't be used.
create function public.notify_video_status() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.is_draft then
    return null;
  end if;

  if new.status = 'ready' and old.status is distinct from 'ready' then
    perform private.notify(
      new.owner_id, 'video_ready',
      case when new.rating is null then 'Your clip is live'
           else format('Your clip scored %s', trim(to_char(new.rating, 'FM90.9'))) end,
      case when new.rating is null then format('“%s” is on your profile for scouts to watch.', new.title)
           else format('“%s” has been rated by the movement analysis.', new.title) end,
      jsonb_build_object('href', '/(tabs)/videos', 'video_id', new.id)
    );
  elsif new.status = 'rejected' and old.status is distinct from 'rejected' then
    perform private.notify(
      new.owner_id, 'video_rejected',
      'A clip needs another go',
      coalesce(nullif(new.reject_reason, ''), format('“%s” couldn''t be used.', new.title)),
      jsonb_build_object('href', '/video-upload', 'video_id', new.id)
    );
  end if;
  return null;
end;
$$;
revoke execute on function public.notify_video_status() from public, anon, authenticated;

create trigger videos_notify after update of status on public.videos
  for each row execute function public.notify_video_status();

-- Someone applies to a trial, and later the coach decides.
create function public.notify_application() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_trial public.trials;
  v_athlete text;
begin
  select * into v_trial from public.trials where id = new.trial_id;
  if not found then
    return null;
  end if;

  if tg_op = 'INSERT' or (old.status = 'withdrawn' and new.status = 'pending') then
    select coalesce(full_name, 'An athlete') into v_athlete from public.profiles where id = new.athlete_id;
    perform private.notify(
      v_trial.created_by, 'application_received',
      'New applicant',
      format('%s applied to %s.', v_athlete, v_trial.title),
      jsonb_build_object('href', format('/trial/%s', v_trial.id), 'trial_id', v_trial.id)
    );
    return null;
  end if;

  if new.status = old.status then
    return null;
  end if;

  if new.status = 'accepted' then
    perform private.notify(
      new.athlete_id, 'application_accepted',
      format('You''re in: %s', v_trial.title),
      trim(concat_ws(' ',
        format('%s accepted your application.', v_trial.club_name),
        case when new.slot is not null then format('Your slot is %s.', new.slot) end,
        new.decision_note
      )),
      jsonb_build_object('href', format('/trial/%s', v_trial.id), 'trial_id', v_trial.id)
    );
  elsif new.status = 'rejected' and v_trial.status <> 'cancelled' then
    perform private.notify(
      new.athlete_id, 'application_rejected',
      'Not this time',
      trim(concat_ws(' ',
        format('%s isn''t taking you on for %s.', v_trial.club_name, v_trial.title),
        new.decision_note,
        'Keep uploading — other clubs are looking.'
      )),
      jsonb_build_object('href', format('/trial/%s', v_trial.id), 'trial_id', v_trial.id)
    );
  end if;
  return null;
end;
$$;
revoke execute on function public.notify_application() from public, anon, authenticated;

create trigger trial_applications_notify after insert or update of status on public.trial_applications
  for each row execute function public.notify_application();

-- A coach invites an athlete to a trial.
create function public.notify_invite() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_trial public.trials;
  v_coach text;
begin
  if new.status <> 'sent' then
    return null;
  end if;
  select * into v_trial from public.trials where id = new.trial_id;
  select coalesce(full_name, 'A coach') into v_coach from public.profiles where id = new.coach_id;
  perform private.notify(
    new.athlete_id, 'trial_invite',
    'You''re invited to a trial',
    trim(concat_ws(' ',
      format('%s invited you to %s.', v_coach, v_trial.title),
      new.message
    )),
    jsonb_build_object('href', format('/trial/%s', v_trial.id), 'trial_id', v_trial.id)
  );
  return null;
end;
$$;
revoke execute on function public.notify_invite() from public, anon, authenticated;

create trigger trial_invites_notify after insert or update of status on public.trial_invites
  for each row execute function public.notify_invite();

-- A trial is called off: everyone still in the running hears about it.
create function public.notify_trial_cancelled() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_athlete uuid;
begin
  if new.status <> 'cancelled' or old.status = 'cancelled' then
    return null;
  end if;
  for v_athlete in
    select athlete_id from public.trial_applications
     where trial_id = new.id and status <> 'withdrawn'
  loop
    perform private.notify(
      v_athlete, 'trial_cancelled',
      'Trial called off',
      format('%s cancelled %s on %s.', new.club_name, new.title, to_char(new.trial_date, 'FMDD Mon')),
      jsonb_build_object('href', format('/trial/%s', new.id), 'trial_id', new.id)
    );
  end loop;
  return null;
end;
$$;
revoke execute on function public.notify_trial_cancelled() from public, anon, authenticated;

create trigger trials_notify_cancelled after update of status on public.trials
  for each row execute function public.notify_trial_cancelled();

-- One coach passes an athlete to another.
create function public.notify_recommendation() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_from text;
  v_athlete text;
begin
  select coalesce(full_name, 'A coach') into v_from from public.profiles where id = new.from_coach_id;
  select coalesce(full_name, 'an athlete') into v_athlete from public.profiles where id = new.athlete_id;
  perform private.notify(
    new.to_coach_id, 'recommendation',
    'A coach sent you a player',
    trim(concat_ws(' ',
      format('%s recommended %s.', v_from, v_athlete),
      new.note,
      case when new.suggest_trial then 'They think they''re worth a trial.' end
    )),
    jsonb_build_object('href', format('/talent/%s', new.athlete_id), 'athlete_id', new.athlete_id)
  );
  return null;
end;
$$;
revoke execute on function public.notify_recommendation() from public, anon, authenticated;

create trigger recommendations_notify after insert on public.recommendations
  for each row execute function public.notify_recommendation();

-- An admin decides on a coach's verification.
create function public.notify_coach_verification() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.verification_status = old.verification_status then
    return null;
  end if;
  if new.verification_status = 'verified' then
    perform private.notify(
      new.profile_id, 'coach_verified',
      'You''re a verified scout',
      'You can now post trials, invite athletes and search players.',
      jsonb_build_object('href', '/(coach)/profile')
    );
  elsif new.verification_status = 'rejected' then
    perform private.notify(
      new.profile_id, 'coach_rejected',
      'Verification needs another look',
      'We couldn''t confirm your club. Send your details again and we''ll review it.',
      jsonb_build_object('href', '/(coach)/profile')
    );
  end if;
  return null;
end;
$$;
revoke execute on function public.notify_coach_verification() from public, anon, authenticated;

create trigger coach_profiles_notify after update of verification_status on public.coach_profiles
  for each row execute function public.notify_coach_verification();

-- ---------------------------------------------------------------------------------------------
-- push tokens
-- ---------------------------------------------------------------------------------------------

create table public.push_tokens (
  token text primary key check (char_length(token) between 10 and 200),
  user_id uuid not null references public.profiles (id) on delete cascade,
  platform text check (platform in ('ios', 'android')),
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create index push_tokens_user_idx on public.push_tokens (user_id);

revoke all on public.push_tokens from anon, authenticated;
grant select, insert, update, delete on public.push_tokens to authenticated;
grant all on public.push_tokens to service_role;
alter table public.push_tokens enable row level security;

-- A device registers its own token; signing out removes it.
create policy "people see their own devices" on public.push_tokens
  for select to authenticated using (user_id = auth.uid());
create policy "people register their own devices" on public.push_tokens
  for insert to authenticated with check (user_id = auth.uid());
create policy "people refresh their own devices" on public.push_tokens
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "people remove their own devices" on public.push_tokens
  for delete to authenticated using (user_id = auth.uid());

-- ---------------------------------------------------------------------------------------------
-- blocking
-- ---------------------------------------------------------------------------------------------

create table public.blocks (
  blocker_id uuid not null references public.profiles (id) on delete cascade,
  blocked_id uuid not null references public.profiles (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (blocker_id, blocked_id),
  check (blocker_id <> blocked_id)
);

create index blocks_blocked_idx on public.blocks (blocked_id);

revoke all on public.blocks from anon, authenticated;
grant select, insert, delete on public.blocks to authenticated;
grant all on public.blocks to service_role;
alter table public.blocks enable row level security;

create policy "people see who they blocked" on public.blocks
  for select to authenticated using (blocker_id = auth.uid());
create policy "people block for themselves" on public.blocks
  for insert to authenticated with check (blocker_id = auth.uid());
create policy "people unblock for themselves" on public.blocks
  for delete to authenticated using (blocker_id = auth.uid());

-- True when either person has blocked the other. Used to hide clips both ways.
create function private.blocked_with(p_other uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.blocks
     where (blocker_id = auth.uid() and blocked_id = p_other)
        or (blocker_id = p_other and blocked_id = auth.uid())
  )
$$;
revoke execute on function private.blocked_with(uuid) from public, anon;
grant execute on function private.blocked_with(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- reports
-- ---------------------------------------------------------------------------------------------

create type public.report_target as enum ('video', 'profile', 'trial');
create type public.report_status as enum ('open', 'actioned', 'dismissed');

create table public.reports (
  id uuid primary key default gen_random_uuid(),
  reporter_id uuid references public.profiles (id) on delete set null,
  target_type public.report_target not null,
  target_id uuid not null,
  reason text not null check (char_length(reason) between 2 and 60),
  details text check (char_length(details) <= 500),
  status public.report_status not null default 'open',
  reviewed_by uuid references public.profiles (id) on delete set null,
  reviewed_at timestamptz,
  review_note text check (char_length(review_note) <= 300),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index reports_open_idx on public.reports (created_at desc) where status = 'open';
create index reports_target_idx on public.reports (target_type, target_id);
-- One open report per person per thing, so a queue can't be flooded from one account.
create unique index reports_one_open_per_reporter
  on public.reports (reporter_id, target_type, target_id) where status = 'open';

create trigger reports_updated_at before update on public.reports
  for each row execute function public.set_updated_at();

revoke all on public.reports from anon, authenticated;
grant select on public.reports to authenticated;
grant all on public.reports to service_role;
alter table public.reports enable row level security;

-- Reporters see what they sent; admins see everything.
create policy "reporters and admins read reports" on public.reports
  for select to authenticated using (reporter_id = auth.uid() or private.is_admin());

create function public.report_content(
  p_target_type text,
  p_target_id uuid,
  p_reason text,
  p_details text default null
) returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_type public.report_target;
  v_owner uuid;
  v_id uuid;
begin
  if p_target_type not in ('video', 'profile', 'trial') then
    raise exception 'Unknown thing to report.' using errcode = 'P0001';
  end if;
  v_type := p_target_type::public.report_target;

  if v_type = 'video' then
    select owner_id into v_owner from public.videos where id = p_target_id;
  elsif v_type = 'trial' then
    select created_by into v_owner from public.trials where id = p_target_id;
  else
    select id into v_owner from public.profiles where id = p_target_id;
  end if;

  if v_owner is null then
    raise exception 'That no longer exists.' using errcode = 'P0001';
  end if;
  if v_owner = auth.uid() then
    raise exception 'You can''t report your own content.' using errcode = 'P0001';
  end if;
  if nullif(btrim(coalesce(p_reason, '')), '') is null then
    raise exception 'Pick a reason.' using errcode = 'P0001';
  end if;

  insert into public.reports (reporter_id, target_type, target_id, reason, details)
  values (auth.uid(), v_type, p_target_id, btrim(p_reason), nullif(btrim(coalesce(p_details, '')), ''))
  on conflict (reporter_id, target_type, target_id) where status = 'open'
    do update set reason = excluded.reason, details = excluded.details, updated_at = now()
  returning id into v_id;
  return v_id;
end;
$$;
revoke execute on function public.report_content(text, uuid, text, text) from public, anon;
grant execute on function public.report_content(text, uuid, text, text) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Blocked people drop out of the feed and out of search
-- ---------------------------------------------------------------------------------------------

create or replace function public.get_feed(
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
       and not private.blocked_with(v.owner_id)
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
       and not private.blocked_with(t.created_by)
       and (p_before is null or t.created_at < p_before)
     order by t.created_at desc
     limit least(greatest(p_limit, 1), 30)
  )
  select * from (select * from clips union all select * from open_trials) feed
   order by 3 desc
   limit least(greatest(p_limit, 1), 30)
$$;

create or replace function public.search_athletes(
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
       and not private.blocked_with(p.id)
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
