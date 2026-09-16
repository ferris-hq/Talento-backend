-- Identity + RLS tests. Runs inside a transaction that is rolled back, so it leaves no data.
-- Each check raises an exception on failure, so run with psql -v ON_ERROR_STOP=1.

begin;

-- ---- helpers ---------------------------------------------------------------------------------

create function pg_temp.act_as(p_user uuid) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims', json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end;
$$;

create function pg_temp.act_as_service() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
end;
$$;

create function pg_temp.expect(p_ok boolean, p_message text) returns void language plpgsql as $$
begin
  if not coalesce(p_ok, false) then
    raise exception 'FAILED: %', p_message;
  end if;
  raise notice 'ok - %', p_message;
end;
$$;

-- Runs p_sql as the current role and asserts it fails (permission error, RLS violation or guard).
create function pg_temp.expect_denied(p_sql text, p_message text) returns void language plpgsql as $$
begin
  begin
    execute p_sql;
  exception when others then
    raise notice 'ok - % (%)', p_message, sqlerrm;
    return;
  end;
  raise exception 'FAILED: expected denial: %', p_message;
end;
$$;

-- ---- fixtures (as service) -------------------------------------------------------------------

insert into auth.users (id, email, raw_user_meta_data) values
  ('00000000-0000-0000-0000-00000000a001', 'minor@test.dev', '{"full_name":"Kwam Asante"}'),
  ('00000000-0000-0000-0000-00000000a002', 'adult@test.dev', '{"full_name":"Alex Rodriguez"}'),
  ('00000000-0000-0000-0000-00000000c001', 'coach@test.dev', '{"full_name":"Mike Millar"}'),
  ('00000000-0000-0000-0000-00000000c002', 'verified@test.dev', '{"full_name":"Daniel Ofori"}'),
  ('00000000-0000-0000-0000-0000000ad001', 'admin@test.dev', '{"full_name":"Talento Admin"}');

select pg_temp.expect(
  (select count(*) from public.profiles where id::text like '00000000-0000-0000-0000-0000000%') = 5,
  'signup trigger creates a profile per auth user'
);
select pg_temp.expect(
  (select full_name from public.profiles where id = '00000000-0000-0000-0000-00000000a001') = 'Kwam Asante',
  'profile picks up full_name from signup metadata'
);

update public.profiles set role = 'admin' where id = '00000000-0000-0000-0000-0000000ad001';
insert into public.clubs (id, name, region)
  values ('00000000-0000-0000-0000-0000000c1b01', 'Hearts of Oak', 'Greater Accra');

-- ---- onboarding as an athlete (minor) ----------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-00000000a001');

update public.profiles set role = 'athlete', region = 'Ashanti' where id = auth.uid();
select pg_temp.expect(
  (select role from public.profiles where id = auth.uid()) = 'athlete',
  'user can pick a role during onboarding'
);

select pg_temp.expect_denied(
  $$update public.profiles set role = 'coach' where id = auth.uid()$$,
  'role cannot be changed once set'
);

insert into public.athlete_profiles (profile_id, position, visibility)
  values (auth.uid(), 'Forward', 'public');
insert into public.athlete_private (profile_id, date_of_birth)
  values (auth.uid(), current_date - interval '16 years');

select pg_temp.expect(
  (select age_group = 'U-17' and is_minor and not guardian_consented
     from public.athlete_profiles where profile_id = auth.uid()),
  'age group and minor flag are derived from date of birth'
);

select pg_temp.expect_denied(
  $$update public.athlete_profiles set is_minor = false where profile_id = auth.uid()$$,
  'athletes cannot overwrite derived minor flag'
);

select pg_temp.expect_denied(
  $$update public.athlete_private set guardian_consent_at = now() where profile_id = auth.uid()$$,
  'guardian consent requires guardian name and phone'
);

-- ---- another athlete (adult) -----------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-00000000a002');
update public.profiles set role = 'athlete' where id = auth.uid();
insert into public.athlete_profiles (profile_id, position) values (auth.uid(), 'Midfielder');
insert into public.athlete_private (profile_id, date_of_birth) values (auth.uid(), current_date - interval '21 years');

select pg_temp.expect(
  not exists (select 1 from public.athlete_profiles where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'other athletes cannot see a minor without guardian consent'
);
select pg_temp.expect(
  not exists (select 1 from public.athlete_private where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'other users cannot read athlete private data'
);
-- RLS turns updates of rows you can't see into silent no-ops; verified below as the owner.
update public.athlete_profiles set position = 'Goalkeeper'
  where profile_id = '00000000-0000-0000-0000-00000000a001';

select pg_temp.expect_denied(
  $$insert into public.coach_profiles (profile_id) values (auth.uid())$$,
  'athletes cannot create a coach profile'
);

-- ---- minor gets guardian consent -------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-00000000a001');
update public.athlete_private
   set guardian_name = 'Ama Asante', guardian_phone = '+233200000000', guardian_consent_at = now()
 where profile_id = auth.uid();
select pg_temp.expect(
  (select position from public.athlete_profiles where profile_id = auth.uid()) = 'Forward',
  'other users cannot update someone else''s athlete profile'
);

select pg_temp.act_as('00000000-0000-0000-0000-00000000a002');
select pg_temp.expect(
  exists (select 1 from public.athlete_profiles where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'minor with guardian consent and public visibility becomes visible'
);

-- ---- coaches and verification ----------------------------------------------------------------

select pg_temp.act_as('00000000-0000-0000-0000-00000000c001');
update public.profiles set role = 'coach' where id = auth.uid();
insert into public.coach_profiles (profile_id, organization_text, focus_sport)
  values (auth.uid(), 'Hearts of Oak Academy', 'football');

select pg_temp.expect_denied(
  $$update public.coach_profiles set verification_status = 'verified' where profile_id = auth.uid()$$,
  'coaches cannot verify themselves'
);
select pg_temp.expect_denied(
  $$update public.profiles set role = 'admin' where id = auth.uid()$$,
  'users cannot grant themselves admin'
);

-- Minor switches to scouts-only; an unverified coach loses sight of them.
select pg_temp.act_as('00000000-0000-0000-0000-00000000a001');
update public.athlete_profiles set visibility = 'scouts_only' where profile_id = auth.uid();

select pg_temp.act_as('00000000-0000-0000-0000-00000000c001');
select pg_temp.expect(
  not exists (select 1 from public.athlete_profiles where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'unverified coaches cannot see scouts-only athletes'
);

insert into public.coach_verification_requests (coach_id, club_name_submitted, role_at_club, documents)
  values (auth.uid(), 'Hearts of Oak', 'Academy coach', array['private/verification/c001/id.jpg']);
select pg_temp.expect(
  (select verification_status from public.coach_profiles where profile_id = auth.uid()) = 'pending',
  'submitting verification marks the coach pending'
);
select pg_temp.expect_denied(
  $$insert into public.coach_verification_requests (coach_id) values (auth.uid())$$,
  'only one pending verification request per coach'
);
select pg_temp.expect_denied(
  $$update public.coach_verification_requests set status = 'approved' where coach_id = auth.uid()$$,
  'coaches cannot approve their own verification request'
);

-- Second coach cannot read the first coach's request.
select pg_temp.act_as('00000000-0000-0000-0000-00000000c002');
select pg_temp.expect_denied(
  $$update public.profiles set role = 'admin' where id = auth.uid()$$,
  'a new user cannot pick admin as their role'
);
update public.profiles set role = 'coach' where id = auth.uid();
insert into public.coach_profiles (profile_id, focus_sport) values (auth.uid(), 'football');
select pg_temp.expect(
  not exists (select 1 from public.coach_verification_requests where coach_id = '00000000-0000-0000-0000-00000000c001'),
  'coaches cannot read other coaches'' verification requests'
);

-- Admin can read all requests.
select pg_temp.act_as('00000000-0000-0000-0000-0000000ad001');
select pg_temp.expect(
  exists (select 1 from public.coach_verification_requests where coach_id = '00000000-0000-0000-0000-00000000c001'),
  'admins can read verification requests'
);
select pg_temp.expect(
  exists (select 1 from public.athlete_private where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'admins can read athlete private data'
);

-- Service reviews and approves (what the admin API endpoint will do).
select pg_temp.act_as_service();
update public.coach_verification_requests
   set status = 'approved',
       club_id = '00000000-0000-0000-0000-0000000c1b01',
       reviewed_by = '00000000-0000-0000-0000-0000000ad001',
       reviewed_at = now()
 where coach_id = '00000000-0000-0000-0000-00000000c001' and status = 'pending';

select pg_temp.expect(
  (select verification_status = 'verified' and club_id is not null and verified_at is not null
     from public.coach_profiles where profile_id = '00000000-0000-0000-0000-00000000c001'),
  'approving a request verifies the coach and links the club'
);

select pg_temp.act_as('00000000-0000-0000-0000-00000000c001');
select pg_temp.expect(
  private.is_verified_coach(),
  'private.is_verified_coach() is true for the approved coach'
);
select pg_temp.expect(
  exists (select 1 from public.athlete_profiles where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'verified coaches can see scouts-only athletes'
);
select pg_temp.expect(
  not exists (select 1 from public.athlete_private where profile_id = '00000000-0000-0000-0000-00000000a001'),
  'verified coaches still cannot read athlete private data'
);

-- ---- anon ------------------------------------------------------------------------------------

select pg_temp.act_as_service();
set local role anon;
select pg_temp.expect_denied(
  $$select 1 from public.profiles limit 1$$,
  'anonymous requests cannot read profiles'
);

-- ---- helpers are not callable anonymously -----------------------------------------------------

select pg_temp.act_as_service();
set local role anon;
select pg_temp.expect_denied(
  $$select private.is_admin()$$,
  'anonymous requests cannot call private helpers'
);
reset role;
select pg_temp.act_as('00000000-0000-0000-0000-00000000a002');
select pg_temp.expect_denied(
  $$select public.handle_new_user()$$,
  'signed-in users cannot call trigger functions directly'
);
reset role;

-- ---- age group helper ------------------------------------------------------------------------

reset role;
select pg_temp.expect(public.age_group_for(date '2010-06-01', date '2026-09-15') = 'U-17', 'age_group_for: 16 -> U-17');
select pg_temp.expect(public.age_group_for(date '2005-01-01', date '2026-09-15') = 'Senior', 'age_group_for: 21 -> Senior');
select pg_temp.expect(public.age_group_for(date '2014-09-16', date '2026-09-15') = 'U-13', 'age_group_for: 11 -> U-13');

rollback;
