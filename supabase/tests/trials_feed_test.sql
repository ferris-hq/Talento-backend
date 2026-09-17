-- Trials, applications, likes/saves/views and the feed. Rolled back at the end.
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

-- people: coach (verified), footballer aged 16, footballer aged 25, boxer aged 16, minor footballer without consent
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-00000000c0a1', 'coach@t.dev'),
  ('00000000-0000-0000-0000-00000000a016', 'f16@t.dev'),
  ('00000000-0000-0000-0000-00000000a025', 'f25@t.dev'),
  ('00000000-0000-0000-0000-00000000b016', 'b16@t.dev'),
  ('00000000-0000-0000-0000-00000000e015', 'minor@t.dev');
update public.profiles set role = 'coach', full_name = 'Coach' where id = '00000000-0000-0000-0000-00000000c0a1';
update public.profiles set role = 'athlete', sport = 'football', full_name = 'Kofi Sixteen', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-00000000a016';
update public.profiles set role = 'athlete', sport = 'football', full_name = 'Ama Twentyfive'
 where id = '00000000-0000-0000-0000-00000000a025';
update public.profiles set role = 'athlete', sport = 'boxing', full_name = 'Boxer'
 where id = '00000000-0000-0000-0000-00000000b016';
update public.profiles set role = 'athlete', sport = 'football', full_name = 'Hidden Minor'
 where id = '00000000-0000-0000-0000-00000000e015';
insert into public.coach_profiles (profile_id, verification_status)
values ('00000000-0000-0000-0000-00000000c0a1', 'verified');
insert into public.athlete_profiles (profile_id, position)
values ('00000000-0000-0000-0000-00000000a016', 'Winger'),
       ('00000000-0000-0000-0000-00000000a025', 'Striker'),
       ('00000000-0000-0000-0000-00000000b016', null),
       ('00000000-0000-0000-0000-00000000e015', 'Defender');
insert into public.athlete_private (profile_id, date_of_birth, guardian_name, guardian_phone, guardian_consent_at) values
  ('00000000-0000-0000-0000-00000000a016', current_date - interval '16 years', 'Mum', '+233200000001', now()),
  ('00000000-0000-0000-0000-00000000a025', current_date - interval '25 years', null, null, null),
  ('00000000-0000-0000-0000-00000000b016', current_date - interval '16 years', 'Dad', '+233200000002', now()),
  ('00000000-0000-0000-0000-00000000e015', current_date - interval '15 years', null, null, null);

insert into public.trials (id, created_by, club_name, title, sport, age_group, venue, trial_date,
                           application_deadline, capacity, created_at) values
  ('00000000-0000-0000-0000-0000000071a1', '00000000-0000-0000-0000-00000000c0a1', 'Hearts of Oak',
   'U-17 winger trial', 'football', 'U-17', 'Accra Sports Stadium', current_date + 10, current_date + 5, 1,
   now() - interval '1 hour'),
  ('00000000-0000-0000-0000-0000000071a2', '00000000-0000-0000-0000-00000000c0a1', 'Hearts of Oak',
   'Closed trial', 'football', null, 'Accra', current_date + 10, current_date - 1, null, now() - interval '2 hours'),
  ('00000000-0000-0000-0000-0000000071a3', '00000000-0000-0000-0000-00000000c0a1', 'Hearts of Oak',
   'Cancelled trial', 'football', null, 'Accra', current_date + 10, current_date + 5, null, now());
update public.trials set status = 'cancelled' where id = '00000000-0000-0000-0000-0000000071a3';

insert into public.videos (id, owner_id, title, status, is_draft, share_to_feed, playback_key, sport, ready_at) values
  ('00000000-0000-0000-0000-0000000f1d01', '00000000-0000-0000-0000-00000000a025', 'Adult clip', 'ready', false, true, 'v/1/720.mp4', 'football', now() - interval '30 minutes'),
  ('00000000-0000-0000-0000-0000000f1d02', '00000000-0000-0000-0000-00000000e015', 'Minor clip', 'ready', false, true, 'v/2/720.mp4', 'football', now() - interval '20 minutes'),
  ('00000000-0000-0000-0000-0000000f1d03', '00000000-0000-0000-0000-00000000a025', 'Draft', 'ready', true, true, 'v/3/720.mp4', 'football', now()),
  ('00000000-0000-0000-0000-0000000f1d04', '00000000-0000-0000-0000-00000000a025', 'Not shared', 'ready', false, false, 'v/4/720.mp4', 'football', now());

-- ---- applying ----
select pg_temp.act_as('00000000-0000-0000-0000-00000000a025');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1')$$,
  'U-17 players only', 'too old for a U-17 trial');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a2')$$,
  'closed', 'deadline passed');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a3')$$,
  'no longer available', 'cancelled trial');
select pg_temp.expect((select count(*) from public.trials) = 2, 'cancelled trials are hidden from others');

select pg_temp.act_as('00000000-0000-0000-0000-00000000b016');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1')$$,
  'different sport', 'wrong sport');

select pg_temp.act_as('00000000-0000-0000-0000-00000000c0a1');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1')$$,
  'Only athletes', 'coaches cannot apply');
select pg_temp.expect((select count(*) from public.trials) = 3, 'owners still see their cancelled trial');

select pg_temp.act_as('00000000-0000-0000-0000-00000000a016');
select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1', '  Fast winger  ');
select pg_temp.expect(
  (select applicants_count from public.trials where id = '00000000-0000-0000-0000-0000000071a1') = 1,
  'applicant count goes up');
select pg_temp.expect(
  (select message from public.trial_applications where athlete_id = auth.uid()) = 'Fast winger',
  'athlete sees their own application');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1')$$,
  'already applied', 'no duplicate applications');
select pg_temp.expect_error(
  $$update public.trial_applications set status = 'accepted' where athlete_id = auth.uid()$$,
  'permission denied', 'athletes cannot change their own status');

-- full trial
select pg_temp.act_as('00000000-0000-0000-0000-00000000e015');
select pg_temp.expect_error($$select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1')$$,
  'full', 'capacity is enforced');

-- withdraw and re-apply
select pg_temp.act_as('00000000-0000-0000-0000-00000000a016');
select public.withdraw_application((select id from public.trial_applications where athlete_id = auth.uid()));
select pg_temp.expect(
  (select applicants_count from public.trials where id = '00000000-0000-0000-0000-0000000071a1') = 0,
  'withdrawing frees the place');
select pg_temp.expect_error(
  $$select public.withdraw_application((select id from public.trial_applications where athlete_id = auth.uid()))$$,
  'can''t be withdrawn', 'cannot withdraw twice');
select public.apply_to_trial('00000000-0000-0000-0000-0000000071a1');
select pg_temp.expect(
  (select status::text from public.trial_applications where athlete_id = auth.uid()) = 'pending',
  'can re-apply after withdrawing');

-- the coach sees applications to their trial; other athletes don't
select pg_temp.act_as('00000000-0000-0000-0000-00000000c0a1');
select pg_temp.expect((select count(*) from public.trial_applications) = 1, 'trial owner sees applications');
select pg_temp.act_as('00000000-0000-0000-0000-00000000a025');
select pg_temp.expect((select count(*) from public.trial_applications) = 0, 'others don''t see applications');

-- ---- feed ----
select pg_temp.act_as('00000000-0000-0000-0000-00000000a016');
select pg_temp.expect(
  (select array_agg(title order by created_at desc) from public.get_feed())
    = array['Adult clip', 'U-17 winger trial'],
  'feed mixes published clips and open trials; hides drafts, unshared, closed and minors without consent');
select pg_temp.expect(
  (select author_name from public.get_feed('highlight')) = 'Ama Twentyfive', 'clips carry the author name');
select pg_temp.expect(
  (select count(*) from public.get_feed('trial')) = 1 and (select kind from public.get_feed('trial')) = 'trial',
  'trial filter');
select pg_temp.expect(
  (select count(*) from public.get_feed(null, now() - interval '45 minutes')) = 1,
  'cursor pages to older items');

select pg_temp.act_as('00000000-0000-0000-0000-00000000c0a1');
select pg_temp.expect((select count(*) from public.get_feed('highlight')) = 2,
  'verified scouts also see clips of minors without consent');

-- ---- likes, saves, views ----
select pg_temp.act_as('00000000-0000-0000-0000-00000000a016');
insert into public.video_likes (user_id, video_id) values (auth.uid(), '00000000-0000-0000-0000-0000000f1d01');
insert into public.video_saves (user_id, video_id) values (auth.uid(), '00000000-0000-0000-0000-0000000f1d01');
insert into public.trial_saves (user_id, trial_id) values (auth.uid(), '00000000-0000-0000-0000-0000000071a1');
select pg_temp.expect(
  (select liked and saved and likes = 1 from public.get_feed('highlight')), 'likes and saves show in the feed');
select pg_temp.expect((select saved from public.get_feed('trial')), 'saved trials show in the feed');
select pg_temp.expect_error(
  $$insert into public.video_likes (user_id, video_id) values (auth.uid(), '00000000-0000-0000-0000-0000000f1d02')$$,
  'row-level security', 'cannot like a clip you cannot see');
select pg_temp.expect_error(
  $$insert into public.video_likes (user_id, video_id) values ('00000000-0000-0000-0000-00000000a025', '00000000-0000-0000-0000-0000000f1d01')$$,
  'row-level security', 'cannot like on behalf of someone else');

select public.record_video_view('00000000-0000-0000-0000-0000000f1d01');
select public.record_video_view('00000000-0000-0000-0000-0000000f1d01');
select public.record_video_view('00000000-0000-0000-0000-0000000f1d03');
select pg_temp.act_as('00000000-0000-0000-0000-00000000a025');
select public.record_video_view('00000000-0000-0000-0000-0000000f1d01');
select pg_temp.expect(
  (select views from public.videos where id = '00000000-0000-0000-0000-0000000f1d01') = 1,
  'one view per person per day; owners and drafts don''t count');

delete from public.video_likes;  -- only removes own likes (none)
select pg_temp.act_as('00000000-0000-0000-0000-00000000a016');
delete from public.video_likes where video_id = '00000000-0000-0000-0000-0000000f1d01';
reset role;
select pg_temp.expect(
  (select likes from public.videos where id = '00000000-0000-0000-0000-0000000f1d01') = 0, 'unlike updates the count');

rollback;
