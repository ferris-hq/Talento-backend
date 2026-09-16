-- Identity: profiles, athletes (public + private split), clubs, coaches and coach verification.
--
-- Access model
--   * Clients use the anon/authenticated roles through PostgREST and are bound by RLS.
--   * FastAPI uses service_role (bypasses RLS) for privileged writes.
--   * Sensitive athlete data (date of birth, guardian contact) lives in athlete_private, readable
--     only by the athlete and admins. Coaches see the derived age_group / is_minor instead.
--   * Columns users must not change themselves (role after onboarding, verification status,
--     derived fields) are protected with column-level grants and guard triggers.

-- ---------------------------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------------------------

create type public.user_role as enum ('athlete', 'coach', 'admin');
create type public.profile_visibility as enum ('public', 'scouts_only');
create type public.coach_verification_status as enum ('unverified', 'pending', 'verified', 'rejected');
create type public.verification_request_status as enum ('pending', 'approved', 'rejected');

-- ---------------------------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------------------------

create function public.set_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- True for FastAPI (service_role JWT) and for direct database sessions (migrations, psql, cron).
create function public.is_service_request() returns boolean
language sql stable set search_path = '' as $$
  select coalesce(auth.role(), 'anon') = 'service_role'
      or current_setting('request.jwt.claims', true) is null
      or current_setting('request.jwt.claims', true) = ''
$$;

-- Age group labels used by the app: U-13, U-15, U-17, U-19, U-21, Senior.
-- Stored on athlete_profiles and refreshed by refresh_athlete_age_groups() as birthdays pass.
create function public.age_group_for(p_dob date, p_on date default current_date) returns text
language sql immutable set search_path = '' as $$
  select case
    when p_dob is null then null
    when extract(year from age(p_on, p_dob)) < 13 then 'U-13'
    when extract(year from age(p_on, p_dob)) < 15 then 'U-15'
    when extract(year from age(p_on, p_dob)) < 17 then 'U-17'
    when extract(year from age(p_on, p_dob)) < 19 then 'U-19'
    when extract(year from age(p_on, p_dob)) < 21 then 'U-21'
    else 'Senior'
  end
$$;

-- ---------------------------------------------------------------------------------------------
-- profiles (one per auth user)
-- ---------------------------------------------------------------------------------------------

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  role public.user_role,                 -- null until the user picks a role in onboarding
  full_name text check (char_length(full_name) between 2 and 80),
  avatar_key text,                       -- R2 object key
  region text,
  city text,
  sport text,
  onboarded_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger profiles_updated_at before update on public.profiles
  for each row execute function public.set_updated_at();

-- Every new auth user gets a profile row.
create function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, full_name)
  values (new.id, nullif(trim(new.raw_user_meta_data ->> 'full_name'), ''));
  return new;
end;
$$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- Users pick athlete or coach once. Admin is granted only by the service role.
create function public.guard_profile_role() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.role is distinct from old.role and not public.is_service_request() then
    if old.role is not null then
      raise exception 'role cannot be changed after onboarding' using errcode = '42501';
    end if;
    if new.role = 'admin' then
      raise exception 'admin role can only be granted by the service' using errcode = '42501';
    end if;
  end if;
  return new;
end;
$$;

create trigger profiles_guard_role before update on public.profiles
  for each row execute function public.guard_profile_role();

create function public.current_role_is(p_role public.user_role) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.profiles where id = auth.uid() and role = p_role)
$$;

create function public.is_admin() returns boolean
language sql stable security definer set search_path = '' as $$
  select public.current_role_is('admin')
$$;

-- ---------------------------------------------------------------------------------------------
-- clubs
-- ---------------------------------------------------------------------------------------------

create table public.clubs (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 2 and 120),
  badge_key text,
  region text,
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index clubs_name_unique on public.clubs (lower(name));

create trigger clubs_updated_at before update on public.clubs
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------------------------
-- athletes
-- ---------------------------------------------------------------------------------------------

create table public.athlete_profiles (
  profile_id uuid primary key references public.profiles (id) on delete cascade,
  position text,
  preferred_side text check (preferred_side in ('left', 'right', 'both')),
  height_cm smallint check (height_cm between 100 and 230),
  weight_kg smallint check (weight_kg between 25 and 200),
  current_club text,
  visibility public.profile_visibility not null default 'public',
  -- Derived from athlete_private by trigger; not writable by clients.
  age_group text,
  is_minor boolean not null default true,
  guardian_consented boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger athlete_profiles_updated_at before update on public.athlete_profiles
  for each row execute function public.set_updated_at();

create table public.athlete_private (
  profile_id uuid primary key references public.athlete_profiles (profile_id) on delete cascade,
  date_of_birth date not null check (date_of_birth > date '1950-01-01' and date_of_birth < current_date),
  guardian_name text,
  guardian_phone text,
  guardian_consent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint guardian_consent_needs_contact
    check (guardian_consent_at is null or (guardian_name is not null and guardian_phone is not null))
);

create trigger athlete_private_updated_at before update on public.athlete_private
  for each row execute function public.set_updated_at();

create function public.sync_athlete_derived_fields() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  update public.athlete_profiles
     set age_group = public.age_group_for(new.date_of_birth),
         is_minor = extract(year from age(current_date, new.date_of_birth)) < 18,
         guardian_consented = new.guardian_consent_at is not null
   where profile_id = new.profile_id;
  return new;
end;
$$;

create trigger athlete_private_sync after insert or update on public.athlete_private
  for each row execute function public.sync_athlete_derived_fields();

-- Call daily (pg_cron or the API scheduler) so age groups roll over on birthdays.
create function public.refresh_athlete_age_groups() returns integer
language sql security definer set search_path = '' as $$
  with updated as (
    update public.athlete_profiles ap
       set age_group = public.age_group_for(p.date_of_birth),
           is_minor = extract(year from age(current_date, p.date_of_birth)) < 18
      from public.athlete_private p
     where p.profile_id = ap.profile_id
       and (ap.age_group is distinct from public.age_group_for(p.date_of_birth)
            or ap.is_minor is distinct from (extract(year from age(current_date, p.date_of_birth)) < 18))
    returning 1
  )
  select count(*)::integer from updated
$$;

-- Minors without guardian consent are only visible to verified coaches, whatever they choose.
create function public.athlete_is_public(p_profile_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.athlete_profiles
     where profile_id = p_profile_id
       and visibility = 'public'
       and (not is_minor or guardian_consented)
  )
$$;

-- ---------------------------------------------------------------------------------------------
-- coaches and verification
-- ---------------------------------------------------------------------------------------------

create table public.coach_profiles (
  profile_id uuid primary key references public.profiles (id) on delete cascade,
  club_id uuid references public.clubs (id) on delete set null,
  organization_text text check (char_length(organization_text) <= 120),
  focus_sport text,
  verification_status public.coach_verification_status not null default 'unverified',
  verified_at timestamptz,
  verified_by uuid references public.profiles (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger coach_profiles_updated_at before update on public.coach_profiles
  for each row execute function public.set_updated_at();

create function public.is_verified_coach(p_profile_id uuid default auth.uid()) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.coach_profiles
     where profile_id = p_profile_id and verification_status = 'verified'
  )
$$;

create table public.coach_verification_requests (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.coach_profiles (profile_id) on delete cascade,
  club_id uuid references public.clubs (id) on delete set null,
  club_name_submitted text,
  role_at_club text,
  documents text[] not null default '{}',   -- R2 keys in the private bucket
  notes text check (char_length(notes) <= 1000),
  status public.verification_request_status not null default 'pending',
  review_notes text,
  reviewed_by uuid references public.profiles (id) on delete set null,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- One open request per coach at a time.
create unique index coach_verification_one_pending
  on public.coach_verification_requests (coach_id) where status = 'pending';

create trigger coach_verification_requests_updated_at before update on public.coach_verification_requests
  for each row execute function public.set_updated_at();

-- Submitting a request marks the coach pending; a review sets the final status.
create function public.sync_coach_verification() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    update public.coach_profiles
       set verification_status = 'pending'
     where profile_id = new.coach_id and verification_status <> 'verified';
  elsif new.status is distinct from old.status then
    update public.coach_profiles
       set verification_status = case new.status
             when 'approved' then 'verified'::public.coach_verification_status
             when 'rejected' then 'rejected'::public.coach_verification_status
             else verification_status
           end,
           verified_at = case when new.status = 'approved' then now() else verified_at end,
           verified_by = case when new.status = 'approved' then new.reviewed_by else verified_by end,
           club_id = case when new.status = 'approved' then coalesce(new.club_id, club_id) else club_id end
     where profile_id = new.coach_id;
  end if;
  return new;
end;
$$;

create trigger coach_verification_sync after insert or update on public.coach_verification_requests
  for each row execute function public.sync_coach_verification();

-- ---------------------------------------------------------------------------------------------
-- Privileges: column-level grants for client writes (service_role keeps full access)
-- ---------------------------------------------------------------------------------------------

revoke all on public.profiles, public.clubs, public.athlete_profiles, public.athlete_private,
  public.coach_profiles, public.coach_verification_requests from anon, authenticated;

grant select on public.profiles, public.clubs, public.athlete_profiles, public.coach_profiles
  to authenticated;
grant update (role, full_name, avatar_key, region, city, sport, onboarded_at)
  on public.profiles to authenticated;

grant insert (profile_id, position, preferred_side, height_cm, weight_kg, current_club, visibility),
      update (position, preferred_side, height_cm, weight_kg, current_club, visibility)
  on public.athlete_profiles to authenticated;

grant select,
      insert (profile_id, date_of_birth, guardian_name, guardian_phone, guardian_consent_at),
      update (date_of_birth, guardian_name, guardian_phone, guardian_consent_at)
  on public.athlete_private to authenticated;

grant insert (profile_id, club_id, organization_text, focus_sport),
      update (club_id, organization_text, focus_sport)
  on public.coach_profiles to authenticated;

grant select,
      insert (coach_id, club_id, club_name_submitted, role_at_club, documents, notes)
  on public.coach_verification_requests to authenticated;

grant all on public.profiles, public.clubs, public.athlete_profiles, public.athlete_private,
  public.coach_profiles, public.coach_verification_requests to service_role;

revoke execute on function public.refresh_athlete_age_groups() from public, anon, authenticated;

-- ---------------------------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------------------------

alter table public.profiles enable row level security;
alter table public.clubs enable row level security;
alter table public.athlete_profiles enable row level security;
alter table public.athlete_private enable row level security;
alter table public.coach_profiles enable row level security;
alter table public.coach_verification_requests enable row level security;

-- profiles: name/avatar/region are shown across the app (feed, trials, coach cards).
create policy "profiles readable by signed-in users" on public.profiles
  for select to authenticated using (true);
create policy "users update own profile" on public.profiles
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());

-- clubs: readable by everyone signed in; managed by the service/admins.
create policy "clubs readable by signed-in users" on public.clubs
  for select to authenticated using (true);

-- athlete_profiles
create policy "athlete profiles visible to owner, scouts, admins or public" on public.athlete_profiles
  for select to authenticated using (
    profile_id = auth.uid()
    or public.is_verified_coach()
    or public.is_admin()
    or public.athlete_is_public(profile_id)
  );
create policy "athletes create own athlete profile" on public.athlete_profiles
  for insert to authenticated with check (
    profile_id = auth.uid() and public.current_role_is('athlete')
  );
create policy "athletes update own athlete profile" on public.athlete_profiles
  for update to authenticated using (profile_id = auth.uid()) with check (profile_id = auth.uid());

-- athlete_private: owner and admins only.
create policy "athlete private readable by owner and admins" on public.athlete_private
  for select to authenticated using (profile_id = auth.uid() or public.is_admin());
create policy "athletes create own private record" on public.athlete_private
  for insert to authenticated with check (profile_id = auth.uid());
create policy "athletes update own private record" on public.athlete_private
  for update to authenticated using (profile_id = auth.uid()) with check (profile_id = auth.uid());

-- coach_profiles: public within the app (athletes should see who a coach is and if verified).
create policy "coach profiles readable by signed-in users" on public.coach_profiles
  for select to authenticated using (true);
create policy "coaches create own coach profile" on public.coach_profiles
  for insert to authenticated with check (
    profile_id = auth.uid() and public.current_role_is('coach')
  );
create policy "coaches update own coach profile" on public.coach_profiles
  for update to authenticated using (profile_id = auth.uid()) with check (profile_id = auth.uid());

-- coach_verification_requests: coach submits and reads own; admins read all. Reviews go through the API.
create policy "coaches and admins read verification requests" on public.coach_verification_requests
  for select to authenticated using (coach_id = auth.uid() or public.is_admin());
create policy "coaches submit own verification request" on public.coach_verification_requests
  for insert to authenticated with check (coach_id = auth.uid());
