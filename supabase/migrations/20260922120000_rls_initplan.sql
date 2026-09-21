-- Phase 8: evaluate auth.uid() once per query instead of once per row.
--
-- Postgres re-runs a plain `auth.uid()` in a policy for every row it checks. Wrapping it in a
-- scalar subquery lets the planner run it once and reuse the answer, which is the difference
-- between a feed query that stays quick as the table grows and one that doesn't. The rules
-- themselves are unchanged: each policy allows and denies exactly what it did before.
--
-- Generated from pg_policies and checked by hand.

alter policy "athlete private readable by owner and admins" on public.athlete_private
  using (((profile_id = (select auth.uid())) OR private.is_admin()));
alter policy "athletes create own private record" on public.athlete_private
  with check ((profile_id = (select auth.uid())));
alter policy "athletes update own private record" on public.athlete_private
  using ((profile_id = (select auth.uid())))
  with check ((profile_id = (select auth.uid())));
alter policy "athlete profiles visible to owner, scouts, admins or public" on public.athlete_profiles
  using (((profile_id = (select auth.uid())) OR private.is_verified_coach() OR private.is_admin() OR private.athlete_is_public(profile_id)));
alter policy "athletes create own athlete profile" on public.athlete_profiles
  with check (((profile_id = (select auth.uid())) AND private.current_role_is('athlete'::public.user_role)));
alter policy "athletes update own athlete profile" on public.athlete_profiles
  using ((profile_id = (select auth.uid())))
  with check ((profile_id = (select auth.uid())));
alter policy "people block for themselves" on public.blocks
  with check ((blocker_id = (select auth.uid())));
alter policy "people see who they blocked" on public.blocks
  using ((blocker_id = (select auth.uid())));
alter policy "people unblock for themselves" on public.blocks
  using ((blocker_id = (select auth.uid())));
alter policy "coaches create own coach profile" on public.coach_profiles
  with check (((profile_id = (select auth.uid())) AND private.current_role_is('coach'::public.user_role)));
alter policy "coaches update own coach profile" on public.coach_profiles
  using ((profile_id = (select auth.uid())))
  with check ((profile_id = (select auth.uid())));
alter policy "coaches change their own ratings" on public.coach_ratings
  using ((coach_id = (select auth.uid())))
  with check ((coach_id = (select auth.uid())));
alter policy "coaches delete their own ratings" on public.coach_ratings
  using ((coach_id = (select auth.uid())));
alter policy "coaches read their own ratings" on public.coach_ratings
  using ((coach_id = (select auth.uid())));
alter policy "scouts rate athletes they can see" on public.coach_ratings
  with check (((coach_id = (select auth.uid())) AND private.is_scout() AND private.can_view_athlete(athlete_id)));
alter policy "coaches and admins read verification requests" on public.coach_verification_requests
  using (((coach_id = (select auth.uid())) OR private.is_admin()));
alter policy "coaches submit own verification request" on public.coach_verification_requests
  with check ((coach_id = (select auth.uid())));
alter policy "people mark their own notifications read" on public.notifications
  using ((user_id = (select auth.uid())))
  with check ((user_id = (select auth.uid())));
alter policy "people read their own notifications" on public.notifications
  using ((user_id = (select auth.uid())));
alter policy "users update own profile" on public.profiles
  using ((id = (select auth.uid())))
  with check ((id = (select auth.uid())));
alter policy "people refresh their own devices" on public.push_tokens
  using ((user_id = (select auth.uid())))
  with check ((user_id = (select auth.uid())));
alter policy "people register their own devices" on public.push_tokens
  with check ((user_id = (select auth.uid())));
alter policy "people remove their own devices" on public.push_tokens
  using ((user_id = (select auth.uid())));
alter policy "people see their own devices" on public.push_tokens
  using ((user_id = (select auth.uid())));
alter policy "recipients mark recommendations read" on public.recommendations
  using ((to_coach_id = (select auth.uid())))
  with check ((to_coach_id = (select auth.uid())));
alter policy "recommendations visible to both coaches" on public.recommendations
  using (((from_coach_id = (select auth.uid())) OR (to_coach_id = (select auth.uid())) OR private.is_admin()));
alter policy "reporters and admins read reports" on public.reports
  using (((reporter_id = (select auth.uid())) OR private.is_admin()));
alter policy "coaches delete their own notes" on public.scout_notes
  using ((coach_id = (select auth.uid())));
alter policy "coaches edit their own notes" on public.scout_notes
  using ((coach_id = (select auth.uid())))
  with check ((coach_id = (select auth.uid())));
alter policy "coaches read their own notes" on public.scout_notes
  using ((coach_id = (select auth.uid())));
alter policy "scouts write notes on athletes they can see" on public.scout_notes
  with check (((coach_id = (select auth.uid())) AND private.is_scout() AND private.can_view_athlete(athlete_id)));
alter policy "coaches read their own shortlist" on public.shortlists
  using ((coach_id = (select auth.uid())));
alter policy "coaches remove from their own shortlist" on public.shortlists
  using ((coach_id = (select auth.uid())));
alter policy "scouts shortlist athletes they can see" on public.shortlists
  with check (((coach_id = (select auth.uid())) AND private.is_scout() AND private.can_view_athlete(athlete_id)));
alter policy "applications visible to the athlete, the trial owner and admins" on public.trial_applications
  using (((athlete_id = (select auth.uid())) OR private.owns_trial(trial_id) OR private.is_admin()));
alter policy "invites visible to the athlete and the inviting coach" on public.trial_invites
  using (((athlete_id = (select auth.uid())) OR (coach_id = (select auth.uid())) OR private.owns_trial(trial_id) OR private.is_admin()));
alter policy "own trial saves" on public.trial_saves
  using ((user_id = (select auth.uid())))
  with check (((user_id = (select auth.uid())) AND (EXISTS ( SELECT 1    FROM trials t   WHERE (t.id = trial_saves.trial_id)))));
alter policy "trials readable unless cancelled" on public.trials
  using (((status <> 'cancelled'::public.trial_status) OR (created_by = (select auth.uid())) OR private.is_admin()));
alter policy "own video likes" on public.video_likes
  using ((user_id = (select auth.uid())))
  with check (((user_id = (select auth.uid())) AND (EXISTS ( SELECT 1    FROM videos v   WHERE (v.id = video_likes.video_id)))));
alter policy "own video saves" on public.video_saves
  using ((user_id = (select auth.uid())))
  with check (((user_id = (select auth.uid())) AND (EXISTS ( SELECT 1    FROM videos v   WHERE (v.id = video_saves.video_id)))));
alter policy "owners edit video details" on public.videos
  using ((owner_id = (select auth.uid())))
  with check ((owner_id = (select auth.uid())));
alter policy "owners read their videos" on public.videos
  using ((owner_id = (select auth.uid())));


-- ---------------------------------------------------------------------------------------------
-- Indexes for the foreign keys we actually look up by
-- ---------------------------------------------------------------------------------------------

-- Without these, "everything pointing at this row" means a sequential scan, and deleting a
-- profile has to check every child table row by row.
create index if not exists shortlists_athlete_idx on public.shortlists (athlete_id);
create index if not exists scout_notes_athlete_idx on public.scout_notes (athlete_id);
create index if not exists coach_ratings_athlete_idx on public.coach_ratings (athlete_id);
create index if not exists recommendations_athlete_idx on public.recommendations (athlete_id);
create index if not exists trial_invites_coach_idx on public.trial_invites (coach_id);
create index if not exists trial_saves_trial_idx on public.trial_saves (trial_id);
create index if not exists video_saves_video_idx on public.video_saves (video_id);
create index if not exists video_views_viewer_idx on public.video_views (viewer_id);
create index if not exists trial_applications_decided_by_idx on public.trial_applications (decided_by);
create index if not exists trials_club_idx on public.trials (club_id);
create index if not exists reports_reviewed_by_idx on public.reports (reviewed_by);
create index if not exists coach_profiles_club_idx on public.coach_profiles (club_id);
create index if not exists coach_profiles_verified_by_idx on public.coach_profiles (verified_by);
create index if not exists coach_verification_club_idx on public.coach_verification_requests (club_id);
create index if not exists coach_verification_reviewed_by_idx
  on public.coach_verification_requests (reviewed_by);
