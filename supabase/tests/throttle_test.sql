-- Limits on how fast one account can act. Rolled back at the end.
begin;

create function pg_temp.act_as(p_user uuid) returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end;
$$;

create function pg_temp.expect(p_ok boolean, p_message text) returns void language plpgsql as $$
begin
  if not coalesce(p_ok, false) then raise exception 'FAILED: %', p_message; end if;
  raise notice 'ok - %', p_message;
end;
$$;

create function pg_temp.expect_error(p_sql text, p_contains text, p_message text) returns void
language plpgsql as $$
begin
  begin
    execute p_sql;
  exception when others then
    if position(p_contains in sqlerrm) = 0 then
      raise exception 'FAILED: % (got "%")', p_message, sqlerrm;
    end if;
    raise notice 'ok - % (%)', p_message, sqlerrm;
    return;
  end;
  raise exception 'FAILED: expected an error: %', p_message;
end;
$$;

insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'kofi@t.dev'),
  ('00000000-0000-0000-0000-0000000000a2', 'ama@t.dev'),
  ('00000000-0000-0000-0000-0000000000c1', 'coach@t.dev');
update public.profiles set role = 'athlete', full_name = 'Kofi', sport = 'football'
 where id = '00000000-0000-0000-0000-0000000000a1';
update public.profiles set role = 'athlete', full_name = 'Ama', sport = 'football'
 where id = '00000000-0000-0000-0000-0000000000a2';
update public.profiles set role = 'coach', full_name = 'Coach' where id = '00000000-0000-0000-0000-0000000000c1';
insert into public.athlete_profiles (profile_id) values
  ('00000000-0000-0000-0000-0000000000a1'), ('00000000-0000-0000-0000-0000000000a2');
insert into public.athlete_private (profile_id, date_of_birth) values
  ('00000000-0000-0000-0000-0000000000a1', current_date - interval '20 years'),
  ('00000000-0000-0000-0000-0000000000a2', current_date - interval '20 years');
insert into public.coach_profiles (profile_id, verification_status)
values ('00000000-0000-0000-0000-0000000000c1', 'verified');

-- Ama has clips to be reported.
insert into public.videos (owner_id, title, status, share_to_feed, playback_key, poster_key, ready_at)
select '00000000-0000-0000-0000-0000000000a2', 'Clip ' || n, 'ready', true,
       'v/a2/' || n || '.mp4', 'v/a2/' || n || '.jpg', now()
  from generate_series(1, 16) n;

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');

-- Fifteen reports go through; the sixteenth is asked to slow down.
do $$
declare v_id uuid;
begin
  for v_id in select id from public.videos order by title limit 15 loop
    perform public.report_content('video', v_id, 'Not their own footage');
  end loop;
end $$;
select pg_temp.expect(
  (select count(*) from public.reports) = 15,
  'fifteen reports in an hour are allowed'
);

select pg_temp.expect_error(
  format($$select public.report_content('video', %L, 'Not their own footage')$$,
         (select id from public.videos order by title desc limit 1)),
  'a lot in a short time',
  'the sixteenth report in an hour is turned away'
);

-- The limit is per person: someone else is unaffected.
select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect(
  public.report_content('video', (select id from public.videos order by title desc limit 1),
                        'Not their own footage') is not null,
  'another account is not affected by someone else''s limit'
);

-- The limit is per action: reporting a lot doesn't stop you applying.
reset role;
select set_config('request.jwt.claims', '', true);
insert into public.trials (id, created_by, club_name, title, sport, venue, trial_date, application_deadline)
values ('00000000-0000-0000-0000-0000000f0001', '00000000-0000-0000-0000-0000000000c1', 'Lions',
        'Open day', 'football', 'Accra', current_date + 20, current_date + 10);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  public.apply_to_trial('00000000-0000-0000-0000-0000000f0001') is not null,
  'a different action has its own limit'
);

-- Clients can neither call the throttle nor read what it records.
select pg_temp.expect_error(
  $$select private.throttle('report', 1, interval '1 hour')$$,
  'permission denied',
  'the throttle helper is not callable by clients'
);
select pg_temp.expect_error(
  $$select count(*) from private.rate_events$$,
  'permission denied',
  'clients cannot read the rate log'
);

rollback;
