-- Analysis access and the performance summary. Rolled back at the end.
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

-- Six adult U-21 footballers (b1 is "me") and one minor (m1) who hasn't got guardian consent.
insert into auth.users (id, email)
select ('00000000-0000-0000-0000-0000000000b' || n)::uuid, 'b' || n || '@t.dev' from generate_series(1, 6) n;
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000e1', 'minor@t.dev'),
  ('00000000-0000-0000-0000-0000000000e2', 'viewer@t.dev');
update public.profiles set role = 'athlete', sport = 'football'
 where id::text like '00000000-0000-0000-0000-0000000000b%' or id::text like '00000000-0000-0000-0000-0000000000e%';
insert into public.athlete_profiles (profile_id)
select id from public.profiles where id::text like '00000000-0000-0000-0000-0000000000b%' or id::text like '00000000-0000-0000-0000-0000000000e%';
insert into public.athlete_private (profile_id, date_of_birth)
select id, current_date - interval '19 years' from public.profiles where id::text like '00000000-0000-0000-0000-0000000000b%';
insert into public.athlete_private (profile_id, date_of_birth) values
  ('00000000-0000-0000-0000-0000000000e1', current_date - interval '15 years'),
  ('00000000-0000-0000-0000-0000000000e2', current_date - interval '19 years');

-- One rated, published clip each; b1 also has a rated draft that must not count.
insert into public.videos (id, owner_id, title, status, is_draft, analysis_status, rating)
select ('00000000-0000-0000-0000-00000000c00' || n)::uuid, ('00000000-0000-0000-0000-0000000000b' || n)::uuid,
       'clip', 'ready', false, 'done', 4 + n * 0.5
  from generate_series(1, 6) n;
insert into public.videos (id, owner_id, title, status, is_draft, analysis_status) values
  ('00000000-0000-0000-0000-00000000c0d1', '00000000-0000-0000-0000-0000000000b1', 'draft', 'ready', true, 'done'),
  ('00000000-0000-0000-0000-00000000c0e1', '00000000-0000-0000-0000-0000000000e1', 'minor', 'ready', false, 'done');
insert into public.video_analyses (video_id, owner_id, model_version, status, scores, overall)
select ('00000000-0000-0000-0000-00000000c00' || n)::uuid, ('00000000-0000-0000-0000-0000000000b' || n)::uuid,
       'test', 'done', jsonb_build_object('speed', 40 + n * 5, 'balance', 50 + n * 5), (45 + n * 5) / 10.0
  from generate_series(1, 6) n;
insert into public.video_analyses (video_id, owner_id, model_version, status, scores, overall) values
  ('00000000-0000-0000-0000-00000000c0d1', '00000000-0000-0000-0000-0000000000b1', 'test', 'done',
   '{"speed": 100, "balance": 100}', 10),
  ('00000000-0000-0000-0000-00000000c0e1', '00000000-0000-0000-0000-0000000000e1', 'test', 'done',
   '{"speed": 70, "balance": 70}', 7);

-- b1 reads their own summary
select pg_temp.act_as('00000000-0000-0000-0000-0000000000b1');
select pg_temp.expect(
  (select get_athlete_performance() -> 'scores') = '{"speed": 45, "balance": 55}',
  'own scores come from published clips only (draft ignored)');
select pg_temp.expect((select (get_athlete_performance() ->> 'overall')::numeric) = 5.0, 'overall is the skill average / 10');
select pg_temp.expect((select (get_athlete_performance() ->> 'rated_clips')::int) = 1, 'counts rated published clips');
select pg_temp.expect((select (get_athlete_performance() ->> 'peer_count')::int) = 5, 'five peers in the same sport and age group');
select pg_temp.expect(
  (select get_athlete_performance() -> 'peer_scores') = '{"speed": 60, "balance": 70}',
  'peer averages exclude the athlete themselves');
select pg_temp.expect((select (get_athlete_performance() ->> 'top_percent')::int) = 100,
  'lowest-rated athlete is in the top 100%');
select pg_temp.expect((select jsonb_array_length(get_athlete_performance() -> 'trend')) = 1, 'weekly trend');

-- analyses follow video visibility
select pg_temp.expect((select count(*) from public.video_analyses) = 7,
  'sees own analyses (incl. draft) and other adults'' published ones, not the minor''s');
select pg_temp.expect_denied(
  $$select public.get_athlete_performance('00000000-0000-0000-0000-0000000000e1')$$,
  'cannot read the performance of a minor without consent');
select pg_temp.expect(
  (select (public.get_athlete_performance('00000000-0000-0000-0000-0000000000b6') ->> 'overall')::numeric) = 7.5,
  'can read a public adult''s performance');
select pg_temp.expect_denied(
  $$insert into public.video_analyses (video_id, owner_id, model_version, status)
    values ('00000000-0000-0000-0000-00000000c001', auth.uid(), 'x', 'done')$$,
  'clients cannot write analyses');
select pg_temp.expect_denied($$select private.current_scores(auth.uid())$$, 'score helper is private');

-- small peer groups hide comparisons
reset role;
select pg_temp.act_as('00000000-0000-0000-0000-0000000000e1');
select pg_temp.expect(
  (select get_athlete_performance() -> 'peer_scores') = 'null' and (select get_athlete_performance() -> 'top_percent') = 'null',
  'no peer comparison with fewer than five peers');

rollback;
