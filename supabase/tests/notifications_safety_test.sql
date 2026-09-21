-- Notifications, blocking and reporting. Rolled back at the end.
begin;

create function pg_temp.act_as(p_user uuid) returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end;
$$;

create function pg_temp.as_service() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
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

-- a verified coach, two athletes
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000c1', 'coach@n.dev'),
  ('00000000-0000-0000-0000-0000000000a1', 'kofi@n.dev'),
  ('00000000-0000-0000-0000-0000000000a2', 'ama@n.dev');
update public.profiles set role = 'coach', full_name = 'Coach One', sport = 'football', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-0000000000c1';
update public.profiles set role = 'athlete', full_name = 'Kofi Winger', sport = 'football', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-0000000000a1';
update public.profiles set role = 'athlete', full_name = 'Ama Striker', sport = 'football', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-0000000000a2';
insert into public.coach_profiles (profile_id, organization_text, verification_status)
values ('00000000-0000-0000-0000-0000000000c1', 'Accra Lions', 'unverified');
insert into public.athlete_profiles (profile_id, position) values
  ('00000000-0000-0000-0000-0000000000a1', 'Winger'),
  ('00000000-0000-0000-0000-0000000000a2', 'Striker');
insert into public.athlete_private (profile_id, date_of_birth) values
  ('00000000-0000-0000-0000-0000000000a1', current_date - interval '20 years'),
  ('00000000-0000-0000-0000-0000000000a2', current_date - interval '21 years');

-- ---------------------------------------------------------------------------------------------
-- notifications follow the events that cause them
-- ---------------------------------------------------------------------------------------------

-- verifying the coach tells them
update public.coach_profiles set verification_status = 'verified', verified_at = now()
 where profile_id = '00000000-0000-0000-0000-0000000000c1';
select pg_temp.expect(
  (select count(*) from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000c1' and type = 'coach_verified') = 1,
  'a coach is told when they are verified'
);

-- a clip going ready tells the athlete, with its score
insert into public.videos (id, owner_id, title, status, share_to_feed, raw_key)
values ('00000000-0000-0000-0000-0000000d0001', '00000000-0000-0000-0000-0000000000a1',
        'Wing play', 'processing', true, 'raw/a1');
update public.videos
   set status = 'ready', rating = 7.4, playback_key = 'v/a1/720.mp4', poster_key = 'v/a1/p.jpg',
       ready_at = now()
 where id = '00000000-0000-0000-0000-0000000d0001';
select pg_temp.expect(
  (select title from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a1' and type = 'video_ready')
    = 'Your clip scored 7.4',
  'a rated clip tells the athlete its score'
);

-- a clip that can't be used says why
update public.videos set status = 'rejected', reject_reason = 'Too dark to see you clearly.'
 where id = '00000000-0000-0000-0000-0000000d0001';
select pg_temp.expect(
  (select body from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a1' and type = 'video_rejected')
    = 'Too dark to see you clearly.',
  'a rejected clip passes on the reason'
);

-- an application notifies the coach, and the decision notifies the athlete
insert into public.trials (id, created_by, club_name, title, sport, venue, trial_date, application_deadline)
values ('00000000-0000-0000-0000-0000000f0001', '00000000-0000-0000-0000-0000000000c1', 'Accra Lions',
        'Open day', 'football', 'Accra', current_date + 20, current_date + 10);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select public.apply_to_trial('00000000-0000-0000-0000-0000000f0001');
select pg_temp.as_service();
select pg_temp.expect(
  (select body from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000c1' and type = 'application_received')
    = 'Kofi Winger applied to Open day.',
  'the coach hears about a new applicant'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select public.decide_application(
  (select id from public.trial_applications limit 1), 'accepted', 'Saturday 9:00', 'Bring boots.'
);
select pg_temp.as_service();
select pg_temp.expect(
  (select body from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a1' and type = 'application_accepted')
    like '%Your slot is Saturday 9:00.%Bring boots.%',
  'an accepted athlete gets their slot and the coach''s note'
);

-- inviting an athlete tells them, including the coach's message
select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select public.invite_to_trial('00000000-0000-0000-0000-0000000f0001',
                              '00000000-0000-0000-0000-0000000000a2', 'You would fit this.');
select pg_temp.as_service();
select pg_temp.expect(
  (select body from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a2' and type = 'trial_invite')
    like '%invited you to Open day.%You would fit this.%',
  'an invited athlete hears from the coach'
);

-- cancelling the trial tells everyone still in the running
update public.trials set status = 'cancelled' where id = '00000000-0000-0000-0000-0000000f0001';
select pg_temp.expect(
  (select count(*) from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a1' and type = 'trial_cancelled') = 1,
  'applicants are told when a trial is called off'
);

-- ---------------------------------------------------------------------------------------------
-- notifications are private, and only the database writes them
-- ---------------------------------------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a2');
select pg_temp.expect(
  (select count(*) from public.notifications
    where user_id = '00000000-0000-0000-0000-0000000000a1') = 0,
  'nobody reads someone else''s notifications'
);
select pg_temp.expect_error(
  $$insert into public.notifications (user_id, type, title)
    values ('00000000-0000-0000-0000-0000000000a1', 'video_ready', 'Fake')$$,
  'permission denied',
  'clients cannot write notifications'
);
select pg_temp.expect_error(
  $$select private.notify('00000000-0000-0000-0000-0000000000a1', 'video_ready', 'Fake')$$,
  'permission denied',
  'the notify helper is not callable by clients'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  public.mark_notifications_read() >= 3,
  'marking everything read returns how many changed'
);
select pg_temp.expect(
  (select count(*) from public.notifications where read_at is null) = 0,
  'nothing is left unread afterwards'
);

-- ---------------------------------------------------------------------------------------------
-- blocking hides people both ways
-- ---------------------------------------------------------------------------------------------

select pg_temp.as_service();
update public.videos set status = 'ready', reject_reason = null where id = '00000000-0000-0000-0000-0000000d0001';
insert into public.videos (owner_id, title, status, share_to_feed, playback_key, poster_key, ready_at)
values ('00000000-0000-0000-0000-0000000000a2', 'Finishing', 'ready', true, 'v/a2/720.mp4', 'v/a2/p.jpg', now());

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a2');
select pg_temp.expect(
  (select count(*) from public.get_feed('highlight') where author_id = '00000000-0000-0000-0000-0000000000a1') = 1,
  'the other athlete''s clip is in the feed to start with'
);

insert into public.blocks (blocker_id, blocked_id)
values ('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  (select count(*) from public.get_feed('highlight') where author_id = '00000000-0000-0000-0000-0000000000a1') = 0,
  'blocking removes their clips from your feed'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  (select count(*) from public.get_feed('highlight') where author_id = '00000000-0000-0000-0000-0000000000a2') = 0,
  'and yours from theirs, without telling them'
);
select pg_temp.expect(
  (select count(*) from public.blocks) = 0,
  'the blocked person cannot see they were blocked'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect(
  (select count(*) from public.search_athletes()) = 2,
  'a coach who blocked nobody still sees both athletes'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a2');
select pg_temp.expect_error(
  $$insert into public.blocks (blocker_id, blocked_id)
    values ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000c1')$$,
  'row-level security',
  'you cannot block on someone else''s behalf'
);

-- ---------------------------------------------------------------------------------------------
-- reporting
-- ---------------------------------------------------------------------------------------------

select pg_temp.expect(
  public.report_content('video', '00000000-0000-0000-0000-0000000d0001', 'Not their clip',
                        'This is footage from a televised match.') is not null,
  'anyone can report a clip'
);
select pg_temp.expect(
  (select count(*) from public.reports where status = 'open') = 1,
  'the report lands in the queue'
);
-- reporting the same thing again updates the open report instead of stacking up
select public.report_content('video', '00000000-0000-0000-0000-0000000d0001', 'Not their clip', 'Second thought.');
select pg_temp.expect(
  (select count(*) from public.reports) = 1
    and (select details from public.reports) = 'Second thought.',
  'reporting the same thing twice updates the open report'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect_error(
  $$select public.report_content('video', '00000000-0000-0000-0000-0000000d0001', 'Mine')$$,
  'your own content',
  'you cannot report your own clip'
);
select pg_temp.expect(
  (select count(*) from public.reports) = 0,
  'a report is not visible to the person reported'
);

rollback;
