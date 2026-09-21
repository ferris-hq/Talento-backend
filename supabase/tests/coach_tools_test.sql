-- Coach tools: shortlists, notes, ratings, athlete search, decisions, recommendations, invites.
-- Rolled back at the end.
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

-- people: two verified coaches, one unverified coach, two footballers and a boxer
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000c1', 'coach1@t.dev'),
  ('00000000-0000-0000-0000-0000000000c2', 'coach2@t.dev'),
  ('00000000-0000-0000-0000-0000000000c3', 'newcoach@t.dev'),
  ('00000000-0000-0000-0000-0000000000a1', 'kofi@t.dev'),
  ('00000000-0000-0000-0000-0000000000a2', 'ama@t.dev'),
  ('00000000-0000-0000-0000-0000000000a3', 'boxer@t.dev');

update public.profiles set role = 'coach', full_name = 'Coach One', region = 'Ashanti', sport = 'football'
 where id = '00000000-0000-0000-0000-0000000000c1';
update public.profiles set role = 'coach', full_name = 'Coach Two', region = 'Greater Accra'
 where id = '00000000-0000-0000-0000-0000000000c2';
update public.profiles set role = 'coach', full_name = 'New Coach' where id = '00000000-0000-0000-0000-0000000000c3';
update public.profiles set role = 'athlete', full_name = 'Kofi Winger', sport = 'football', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-0000000000a1';
update public.profiles set role = 'athlete', full_name = 'Ama Striker', sport = 'football', region = 'Volta'
 where id = '00000000-0000-0000-0000-0000000000a2';
update public.profiles set role = 'athlete', full_name = 'Boxer Boy', sport = 'boxing', region = 'Ashanti'
 where id = '00000000-0000-0000-0000-0000000000a3';

insert into public.coach_profiles (profile_id, organization_text, verification_status, focus_sport) values
  ('00000000-0000-0000-0000-0000000000c1', 'Accra Lions', 'verified', 'football'),
  ('00000000-0000-0000-0000-0000000000c2', 'Kumasi Stars', 'verified', 'football'),
  ('00000000-0000-0000-0000-0000000000c3', 'Nobody FC', 'pending', 'football');

insert into public.athlete_profiles (profile_id, position, current_club) values
  ('00000000-0000-0000-0000-0000000000a1', 'Winger', 'Ashanti Youth'),
  ('00000000-0000-0000-0000-0000000000a2', 'Striker', 'Volta United'),
  ('00000000-0000-0000-0000-0000000000a3', null, null);
insert into public.athlete_private (profile_id, date_of_birth, guardian_name, guardian_phone, guardian_consent_at) values
  ('00000000-0000-0000-0000-0000000000a1', current_date - interval '16 years', 'Mum', '+233200000001', now()),
  ('00000000-0000-0000-0000-0000000000a2', current_date - interval '22 years', null, null, null),
  ('00000000-0000-0000-0000-0000000000a3', current_date - interval '19 years', null, null, null);

-- a rated clip each for the footballers, so search has ratings to work with
insert into public.videos (owner_id, title, status, share_to_feed, rating, playback_key, poster_key, ready_at) values
  ('00000000-0000-0000-0000-0000000000a1', 'Kofi clip', 'ready', true, 7.5, 'v/1/720.mp4', 'v/1/p.jpg', now()),
  ('00000000-0000-0000-0000-0000000000a2', 'Ama clip', 'ready', true, 9.0, 'v/2/720.mp4', 'v/2/p.jpg', now());

-- a trial run by coach one, asking for U-17 wingers
insert into public.trials (id, created_by, club_name, title, sport, position, age_group, venue,
                           trial_date, application_deadline)
values ('00000000-0000-0000-0000-0000000f0001', '00000000-0000-0000-0000-0000000000c1', 'Accra Lions',
        'U-17 open trial', 'football', 'Winger', 'U-17', 'Accra Sports Stadium',
        current_date + 20, current_date + 10);

-- a second trial with no age group, open to any footballer
insert into public.trials (id, created_by, club_name, title, sport, venue, trial_date, application_deadline)
values ('00000000-0000-0000-0000-0000000f0002', '00000000-0000-0000-0000-0000000000c1', 'Accra Lions',
        'Senior open day', 'football', 'Accra Sports Stadium', current_date + 25, current_date + 15);

-- ---------------------------------------------------------------------------------------------
-- search
-- ---------------------------------------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect_error(
  $$select * from public.search_athletes()$$,
  'Only verified coaches',
  'athletes cannot search athletes'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c3');
select pg_temp.expect_error(
  $$select * from public.search_athletes()$$,
  'Only verified coaches',
  'unverified coaches cannot search athletes'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect(
  (select count(*) from public.search_athletes()) = 3,
  'a verified coach sees every athlete, including the minor without an adult profile'
);
select pg_temp.expect(
  (select overall from public.search_athletes(p_query => 'Ama')) = 90,
  'overall is the average AI rating on a 0-100 scale'
);
select pg_temp.expect(
  (select "match" from public.search_athletes(p_query => 'Kofi'))
    > (select "match" from public.search_athletes(p_query => 'Boxer')),
  'a footballer in the coach''s own region matches better than a boxer'
);
select pg_temp.expect(
  (select count(*) from public.search_athletes(p_position => 'Striker')) = 1,
  'position filter narrows the list'
);
select pg_temp.expect(
  (select full_name from public.search_athletes(p_sort => 'rating') limit 1) = 'Ama Striker',
  'sorting by rating puts the best-rated athlete first'
);

-- ---------------------------------------------------------------------------------------------
-- shortlist, notes and ratings stay with the coach who wrote them
-- ---------------------------------------------------------------------------------------------

insert into public.shortlists (coach_id, athlete_id)
values ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000a1');
insert into public.scout_notes (coach_id, athlete_id, body)
values ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000a1', 'Great first touch.');
insert into public.coach_ratings (coach_id, athlete_id, technical, physical, mental)
values ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000a1', 4, 5, 3);

select pg_temp.expect(
  (select shortlisted from public.search_athletes(p_query => 'Kofi')),
  'search flags athletes already on the shortlist'
);
select pg_temp.expect(
  (select count(*) from public.search_athletes(p_shortlisted => true)) = 1,
  'the shortlist filter returns only shortlisted athletes'
);

select pg_temp.expect_error(
  $$insert into public.shortlists (coach_id, athlete_id)
    values ('00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-0000000000a2')$$,
  'row-level security',
  'a coach cannot shortlist on behalf of another coach'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c2');
select pg_temp.expect(
  (select count(*) from public.scout_notes) = 0 and (select count(*) from public.coach_ratings) = 0
    and (select count(*) from public.shortlists) = 0,
  'another coach sees nothing of the first coach''s shortlist, notes or ratings'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  (select count(*) from public.scout_notes) = 0,
  'the athlete never sees notes written about them'
);
select pg_temp.expect_error(
  $$insert into public.shortlists (coach_id, athlete_id)
    values ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000a2')$$,
  'row-level security',
  'athletes cannot shortlist anyone'
);

-- ---------------------------------------------------------------------------------------------
-- deciding on applications
-- ---------------------------------------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select public.apply_to_trial('00000000-0000-0000-0000-0000000f0001', 'Please take a look.');
create temp table kofi_application as select id from public.trial_applications limit 1;

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c2');
select pg_temp.expect_error(
  format($$select public.decide_application(%L, 'accepted')$$, (select id from kofi_application)),
  'Only the coach running this trial',
  'another coach cannot decide on someone else''s applicants'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select public.decide_application((select id from kofi_application), 'accepted', 'Saturday 9:00', 'Bring boots.');
select pg_temp.expect(
  (select status = 'accepted' and slot = 'Saturday 9:00' and decided_by = '00000000-0000-0000-0000-0000000000c1'
     from public.trial_applications limit 1),
  'the trial owner accepts an applicant and sets their slot'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  (select slot = 'Saturday 9:00' and decision_note = 'Bring boots.' from public.trial_applications limit 1),
  'the athlete sees the slot and the coach''s note'
);

-- ---------------------------------------------------------------------------------------------
-- recommendations
-- ---------------------------------------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect_error(
  $$select public.recommend_athlete('00000000-0000-0000-0000-0000000000a1',
                                    '00000000-0000-0000-0000-0000000000c3')$$,
  'not verified',
  'you cannot recommend an athlete to an unverified coach'
);
select pg_temp.expect_error(
  $$select public.recommend_athlete('00000000-0000-0000-0000-0000000000a1',
                                    '00000000-0000-0000-0000-0000000000c1')$$,
  'another coach',
  'you cannot recommend an athlete to yourself'
);
select public.recommend_athlete('00000000-0000-0000-0000-0000000000a1',
                                '00000000-0000-0000-0000-0000000000c2', 'Worth a look.', true);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c2');
select pg_temp.expect(
  (select count(*) from public.recommendations where to_coach_id = auth.uid()) = 1,
  'the recipient sees the recommendation'
);
update public.recommendations set status = 'read' where to_coach_id = auth.uid();
select pg_temp.expect(
  (select status from public.recommendations limit 1) = 'read',
  'the recipient can mark it read'
);
select pg_temp.expect(
  (select count(*) from public.coach_peers()) = 1,
  'coach_peers lists other verified coaches only'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect(
  (select count(*) from public.recommendations) = 0,
  'the athlete never sees they were recommended'
);

-- ---------------------------------------------------------------------------------------------
-- invites
-- ---------------------------------------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c2');
select pg_temp.expect_error(
  $$select public.invite_to_trial('00000000-0000-0000-0000-0000000f0001',
                                  '00000000-0000-0000-0000-0000000000a2')$$,
  'Only the coach running this trial',
  'only the trial owner invites athletes'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect_error(
  $$select public.invite_to_trial('00000000-0000-0000-0000-0000000f0001',
                                  '00000000-0000-0000-0000-0000000000a1')$$,
  'already applied',
  'no inviting someone who already applied'
);
select public.invite_to_trial('00000000-0000-0000-0000-0000000f0002',
                              '00000000-0000-0000-0000-0000000000a2', 'You would fit this.');

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a2');
select pg_temp.expect(
  (select count(*) from public.my_trial_invites() where status = 'sent') = 1,
  'the invited athlete sees the invite'
);
select public.apply_to_trial('00000000-0000-0000-0000-0000000f0002');
select pg_temp.expect(
  (select status from public.my_trial_invites() limit 1) = 'applied',
  'applying closes the invite'
);

select pg_temp.act_as('00000000-0000-0000-0000-0000000000a3');
select pg_temp.expect(
  (select count(*) from public.my_trial_invites()) = 0,
  'invites are not visible to anyone else'
);

rollback;
