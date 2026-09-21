-- Phase 8: limits on what one account can do, and a tidy-up of a leftover grant.
--
-- The app talks to PostgREST directly for most writes, so a limit in the API wouldn't see
-- them. These live next to the writes themselves: every function that creates something
-- another person sees now counts recent attempts and refuses politely when someone is going
-- far faster than a person plausibly would.

create table private.rate_events (
  user_id uuid not null,
  action text not null,
  at timestamptz not null default now()
);

create index rate_events_lookup_idx on private.rate_events (user_id, action, at desc);

revoke all on private.rate_events from public, anon, authenticated;

-- Counts this action in the window and raises when the limit is reached.
create function private.throttle(p_action text, p_limit integer, p_window interval)
returns void
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_user uuid := auth.uid();
  v_count integer;
begin
  if v_user is null then
    return;
  end if;

  -- Old rows are only noise; clear this user's as we go.
  delete from private.rate_events
   where user_id = v_user and action = p_action and at < now() - p_window;

  select count(*) into v_count
    from private.rate_events
   where user_id = v_user and action = p_action;

  if v_count >= p_limit then
    raise exception 'That''s a lot in a short time. Try again in a few minutes.'
      using errcode = 'P0001';
  end if;

  insert into private.rate_events (user_id, action) values (v_user, p_action);
end;
$$;
revoke execute on function private.throttle(text, integer, interval) from public, anon, authenticated;

-- Removes rows nothing needs any more. Call from the API scheduler or pg_cron.
create function private.prune_rate_events() returns integer
language sql volatile security definer set search_path = '' as $$
  with deleted as (
    delete from private.rate_events where at < now() - interval '1 day' returning 1
  )
  select count(*)::integer from deleted
$$;
revoke execute on function private.prune_rate_events() from public, anon, authenticated;

-- ---------------------------------------------------------------------------------------------
-- Apply the limits
-- ---------------------------------------------------------------------------------------------

-- An athlete applying to trials: generous for a keen player, closed to a script.
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
  perform private.throttle('apply', 20, interval '1 hour');

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

-- Reports: enough for a person who sees several bad things in a row, not enough to flood.
create or replace function public.report_content(
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
  perform private.throttle('report', 15, interval '1 hour');

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

-- Invites and recommendations reach another person's phone, so they are the tightest.
create or replace function public.invite_to_trial(p_trial uuid, p_athlete uuid, p_message text default null)
returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_trial public.trials;
  v_id uuid;
begin
  perform private.throttle('invite', 60, interval '1 hour');

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

create or replace function public.recommend_athlete(
  p_athlete uuid,
  p_to_coach uuid,
  p_note text default null,
  p_suggest_trial boolean default false
) returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_id uuid;
begin
  perform private.throttle('recommend', 40, interval '1 hour');

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

-- ---------------------------------------------------------------------------------------------
-- Tidy-up
-- ---------------------------------------------------------------------------------------------

-- Supabase installs this event-trigger function in the public schema, where the linter sees it
-- as callable by anyone signed in (or not). Nothing should call it directly.
do $$
begin
  if exists (select 1 from pg_proc where proname = 'rls_auto_enable') then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end $$;

-- video_views is written only through record_video_view(); a deny-all table with no policies
-- is deliberate, and this comment says so for whoever reads the linter next.
comment on table public.video_views is
  'One row per viewer per clip per day. Written only by record_video_view(); no client policies on purpose.';
