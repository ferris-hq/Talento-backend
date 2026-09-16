-- Video access rules. Rolled back at the end.
begin;

create function pg_temp.act_as(p_user uuid) returns void language plpgsql as $$
begin
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

-- fixtures (service)
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'adult@t.dev'),
  ('00000000-0000-0000-0000-0000000000a2', 'minor@t.dev'),
  ('00000000-0000-0000-0000-0000000000a3', 'viewer@t.dev'),
  ('00000000-0000-0000-0000-0000000000c1', 'scout@t.dev');
update public.profiles set role = 'athlete' where id in (
  '00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-0000000000a3');
update public.profiles set role = 'coach' where id = '00000000-0000-0000-0000-0000000000c1';
insert into public.athlete_profiles (profile_id) values
  ('00000000-0000-0000-0000-0000000000a1'), ('00000000-0000-0000-0000-0000000000a2'), ('00000000-0000-0000-0000-0000000000a3');
insert into public.athlete_private (profile_id, date_of_birth) values
  ('00000000-0000-0000-0000-0000000000a1', current_date - interval '20 years'),
  ('00000000-0000-0000-0000-0000000000a2', current_date - interval '15 years'),
  ('00000000-0000-0000-0000-0000000000a3', current_date - interval '19 years');
insert into public.coach_profiles (profile_id, verification_status) values
  ('00000000-0000-0000-0000-0000000000c1', 'verified');

insert into public.videos (id, owner_id, title, status, is_draft) values
  ('00000000-0000-0000-0000-00000000f001', '00000000-0000-0000-0000-0000000000a1', 'Adult ready', 'ready', false),
  ('00000000-0000-0000-0000-00000000f002', '00000000-0000-0000-0000-0000000000a1', 'Adult draft', 'ready', true),
  ('00000000-0000-0000-0000-00000000f003', '00000000-0000-0000-0000-0000000000a1', 'Adult processing', 'processing', false),
  ('00000000-0000-0000-0000-00000000f004', '00000000-0000-0000-0000-0000000000a2', 'Minor ready', 'ready', false);

select pg_temp.expect(public.video_slots_used('00000000-0000-0000-0000-0000000000a1') = 2,
  'slots count published uploading/processing/ready clips, not drafts');

-- owner
select pg_temp.act_as('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect((select count(*) from public.videos) = 3, 'owner sees all of their own videos');
update public.videos set title = 'Renamed', caption = 'New caption' where id = '00000000-0000-0000-0000-00000000f001';
select pg_temp.expect((select title from public.videos where id = '00000000-0000-0000-0000-00000000f001') = 'Renamed',
  'owner can edit title and caption');
select pg_temp.expect_denied(
  $$update public.videos set status = 'ready' where id = '00000000-0000-0000-0000-00000000f003'$$,
  'owner cannot change processing status');
select pg_temp.expect_denied(
  $$update public.videos set is_draft = false where id = '00000000-0000-0000-0000-00000000f002'$$,
  'owner cannot publish a draft directly (quota is enforced by the API)');
select pg_temp.expect_denied(
  $$insert into public.videos (owner_id, title) values (auth.uid(), 'sneaky')$$,
  'clients cannot insert videos');
select pg_temp.expect_denied(
  $$delete from public.videos where id = '00000000-0000-0000-0000-00000000f001'$$,
  'clients cannot delete videos');
select pg_temp.expect_denied($$select public.video_slots_used(auth.uid())$$, 'quota helper is not callable by clients');

-- another athlete
select pg_temp.act_as('00000000-0000-0000-0000-0000000000a3');
select pg_temp.expect(
  (select array_agg(title order by title) from public.videos) = array['Renamed'],
  'other athletes only see published, ready clips of public adults');
update public.videos set title = 'hijack' where id = '00000000-0000-0000-0000-00000000f001';
reset role;
select pg_temp.expect((select title from public.videos where id = '00000000-0000-0000-0000-00000000f001') = 'Renamed',
  'other users cannot edit someone else''s video');

-- verified scout sees the minor's published clip too
select pg_temp.act_as('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect((select count(*) from public.videos) = 2,
  'verified scouts also see published clips of minors without consent');

rollback;
