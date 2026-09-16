-- Keep security-definer helpers out of the REST API.
--
-- * Policy helpers move to the `private` schema (not exposed by PostgREST). RLS policies reference
--   functions by OID, so existing policies keep working. Only signed-in users may execute them.
-- * Trigger functions can't be called via RPC usefully, but they still show up as endpoints;
--   nobody needs EXECUTE on them for the triggers to fire.

create schema if not exists private;
revoke all on schema private from public, anon;
grant usage on schema private to authenticated, service_role;

alter function public.current_role_is(public.user_role) set schema private;
alter function public.is_verified_coach(uuid) set schema private;
alter function public.athlete_is_public(uuid) set schema private;

-- is_admin calls current_role_is by name, so recreate it against the new schema.
drop policy "athlete profiles visible to owner, scouts, admins or public" on public.athlete_profiles;
drop policy "athlete private readable by owner and admins" on public.athlete_private;
drop policy "coaches and admins read verification requests" on public.coach_verification_requests;
drop function public.is_admin();

create function private.is_admin() returns boolean
language sql stable security definer set search_path = '' as $$
  select private.current_role_is('admin')
$$;

create policy "athlete profiles visible to owner, scouts, admins or public" on public.athlete_profiles
  for select to authenticated using (
    profile_id = auth.uid()
    or private.is_verified_coach()
    or private.is_admin()
    or private.athlete_is_public(profile_id)
  );
create policy "athlete private readable by owner and admins" on public.athlete_private
  for select to authenticated using (profile_id = auth.uid() or private.is_admin());
create policy "coaches and admins read verification requests" on public.coach_verification_requests
  for select to authenticated using (coach_id = auth.uid() or private.is_admin());

revoke execute on all functions in schema private from public, anon;
grant execute on all functions in schema private to authenticated, service_role;

revoke execute on function
  public.handle_new_user(),
  public.guard_profile_role(),
  public.sync_athlete_derived_fields(),
  public.sync_coach_verification(),
  public.set_updated_at()
from public, anon, authenticated;
