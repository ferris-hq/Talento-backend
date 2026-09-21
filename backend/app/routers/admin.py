"""Staff endpoints behind the admin role, used by the Talento admin dashboard.

Every route re-checks `profiles.role = 'admin'` on the caller, so a leaked token from a
normal account can't reach them. The service connects as a privileged database role, so
these queries deliberately see rows that row-level security hides from the apps.
"""

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import DbConnection
from app.deps.auth import CurrentUser
from app.services import cdn, storage

router = APIRouter(prefix="/v1/admin", tags=["admin"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
PAGE = 50
MEDIA_KEYS = {"playback_key", "poster_key"}


async def require_admin(user: CurrentUser, db: DbConnection) -> UUID:
    role = await db.fetchval("select role::text from public.profiles where id = $1", user.id)
    if role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user.id


AdminId = Annotated[UUID, Depends(require_admin)]


# ---------------------------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------------------------


class Overview(BaseModel):
    pending_coaches: int
    athletes: int
    coaches: int
    videos_ready: int
    videos_processing: int
    open_trials: int
    applications_pending: int
    signups_7d: int


@router.get("/overview", response_model=Overview)
async def overview(admin: AdminId, db: DbConnection) -> Overview:
    row = await db.fetchrow(
        """
        select
          (select count(*) from public.coach_profiles
            where verification_status = 'pending') as pending_coaches,
          (select count(*) from public.profiles where role = 'athlete') as athletes,
          (select count(*) from public.profiles where role = 'coach') as coaches,
          (select count(*) from public.videos where status = 'ready') as videos_ready,
          (select count(*) from public.videos
            where status in ('uploading', 'processing')) as videos_processing,
          (select count(*) from public.trials
            where status = 'open' and application_deadline >= current_date) as open_trials,
          (select count(*) from public.trial_applications
            where status = 'pending') as applications_pending,
          (select count(*) from auth.users
            where created_at > now() - interval '7 days') as signups_7d
        """
    )
    return Overview(**dict(row))


# ---------------------------------------------------------------------------------------------
# coach verification
# ---------------------------------------------------------------------------------------------


class CoachOut(BaseModel):
    id: UUID
    full_name: str | None
    email: str | None
    organization: str | None
    focus_sport: str | None
    region: str | None
    verification_status: str
    verified_at: dt.datetime | None
    trials: int
    created_at: dt.datetime
    request_id: UUID | None
    request_note: str | None
    request_club_name: str | None
    requested_at: dt.datetime | None


COACH_COLUMNS = """
  p.id, p.full_name, u.email, cp.organization_text as organization, cp.focus_sport, p.region,
  cp.verification_status::text as verification_status, cp.verified_at, p.created_at,
  (select count(*) from public.trials t where t.created_by = p.id) as trials,
  r.id as request_id, r.notes as request_note, r.club_name_submitted as request_club_name,
  r.created_at as requested_at
"""


@router.get("/coaches", response_model=list[CoachOut])
async def list_coaches(
    admin: AdminId,
    db: DbConnection,
    status_filter: Literal["pending", "verified", "rejected", "unverified", "all"] = Query(
        "pending", alias="status"
    ),
    search: str = "",
    limit: int = Query(PAGE, ge=1, le=200),
) -> list[CoachOut]:
    rows = await db.fetch(
        f"""select {COACH_COLUMNS}
              from public.coach_profiles cp
              join public.profiles p on p.id = cp.profile_id
              left join auth.users u on u.id = p.id
              left join lateral (
                select * from public.coach_verification_requests cvr
                 where cvr.coach_id = cp.profile_id
                 order by cvr.created_at desc limit 1
              ) r on true
             where ($1 = 'all' or cp.verification_status::text = $1)
               and ($2 = '' or p.full_name ilike '%' || $2 || '%'
                    or u.email ilike '%' || $2 || '%'
                    or cp.organization_text ilike '%' || $2 || '%')
             order by cp.verification_status = 'pending' desc, r.created_at desc nulls last,
                      p.created_at desc
             limit $3""",
        status_filter,
        search.strip(),
        limit,
    )
    return [CoachOut(**dict(row)) for row in rows]


class VerificationDecision(BaseModel):
    status: Literal["verified", "rejected", "unverified"]
    note: str | None = Field(default=None, max_length=300)


@router.post("/coaches/{coach_id}/verification", response_model=CoachOut)
async def decide_verification(
    coach_id: UUID, body: VerificationDecision, admin: AdminId, db: DbConnection
) -> CoachOut:
    """Approve or reject a coach. Rejecting also closes any open trials they posted."""
    async with db.transaction():
        updated = await db.fetchval(
            """update public.coach_profiles
                  set verification_status = $2::public.coach_verification_status,
                      verified_at = case when $2 = 'verified' then now() end,
                      verified_by = case when $2 = 'verified' then $3::uuid end
                where profile_id = $1
              returning profile_id""",
            coach_id,
            body.status,
            admin,
        )
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Coach not found")
        await db.execute(
            """update public.coach_verification_requests
                  set status = case when $2 = 'verified'
                                    then 'approved'::public.verification_request_status
                                    else 'rejected'::public.verification_request_status end,
                      reviewed_by = $3, reviewed_at = now(),
                      notes = coalesce($4, notes)
                where coach_id = $1 and status = 'pending'""",
            coach_id,
            body.status,
            admin,
            body.note,
        )
        if body.status != "verified":
            # An unverified coach can't run trials, so stop new applications coming in.
            await db.execute(
                """update public.trials set status = 'closed'
                    where created_by = $1 and status = 'open'""",
                coach_id,
            )
    row = await db.fetchrow(
        f"""select {COACH_COLUMNS}
              from public.coach_profiles cp
              join public.profiles p on p.id = cp.profile_id
              left join auth.users u on u.id = p.id
              left join lateral (
                select * from public.coach_verification_requests cvr
                 where cvr.coach_id = cp.profile_id order by cvr.created_at desc limit 1
              ) r on true
             where cp.profile_id = $1""",
        coach_id,
    )
    return CoachOut(**dict(row))


# ---------------------------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------------------------


class UserOut(BaseModel):
    id: UUID
    email: str | None
    full_name: str | None
    role: str | None
    sport: str | None
    region: str | None
    age_group: str | None
    is_minor: bool | None
    guardian_consented: bool | None
    verification_status: str | None
    videos: int
    created_at: dt.datetime
    last_sign_in_at: dt.datetime | None
    suspended: bool


USER_COLUMNS = """
  p.id, u.email, p.full_name, p.role::text as role, p.sport, p.region,
  ap.age_group, ap.is_minor, ap.guardian_consented,
  cp.verification_status::text as verification_status,
  (select count(*) from public.videos v where v.owner_id = p.id) as videos,
  p.created_at, u.last_sign_in_at,
  coalesce(u.banned_until > now(), false) as suspended
"""


@router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: AdminId,
    db: DbConnection,
    role: Literal["athlete", "coach", "admin", "all"] = "all",
    search: str = "",
    limit: int = Query(PAGE, ge=1, le=200),
) -> list[UserOut]:
    rows = await db.fetch(
        f"""select {USER_COLUMNS}
              from public.profiles p
              left join auth.users u on u.id = p.id
              left join public.athlete_profiles ap on ap.profile_id = p.id
              left join public.coach_profiles cp on cp.profile_id = p.id
             where ($1 = 'all' or p.role::text = $1)
               and ($2 = '' or p.full_name ilike '%' || $2 || '%' or u.email ilike '%' || $2 || '%')
             order by p.created_at desc
             limit $3""",
        role,
        search.strip(),
        limit,
    )
    return [UserOut(**dict(row)) for row in rows]


class VideoOut(BaseModel):
    id: UUID
    title: str
    status: str
    analysis_status: str | None
    rating: float | None
    is_draft: bool
    share_to_feed: bool
    views: int
    likes: int
    playback_url: str | None
    poster_url: str | None
    created_at: dt.datetime


class UserDetail(UserOut):
    position: str | None
    date_of_birth: dt.date | None
    guardian_name: str | None
    guardian_phone: str | None
    organization: str | None
    applications: int
    trials: int
    videos_list: list[VideoOut]


@router.get("/users/{user_id}", response_model=UserDetail)
async def read_user(user_id: UUID, admin: AdminId, db: DbConnection) -> UserDetail:
    row = await db.fetchrow(
        f"""select {USER_COLUMNS}, ap.position, apr.date_of_birth, apr.guardian_name,
                   apr.guardian_phone, cp.organization_text as organization,
                   (select count(*) from public.trial_applications ta
                     where ta.athlete_id = p.id) as applications,
                   (select count(*) from public.trials t where t.created_by = p.id) as trials
              from public.profiles p
              left join auth.users u on u.id = p.id
              left join public.athlete_profiles ap on ap.profile_id = p.id
              left join public.athlete_private apr on apr.profile_id = p.id
              left join public.coach_profiles cp on cp.profile_id = p.id
             where p.id = $1""",
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    videos = await db.fetch(
        """select id, title, status::text as status, analysis_status::text as analysis_status,
                  rating, is_draft, share_to_feed, views, likes, playback_key, poster_key,
                  created_at
             from public.videos where owner_id = $1 order by created_at desc limit 50""",
        user_id,
    )
    return UserDetail(
        **dict(row),
        videos_list=[
            VideoOut(
                **{k: v for k, v in dict(video).items() if k not in MEDIA_KEYS},
                playback_url=storage.public_url(video["playback_key"]),
                poster_url=storage.public_url(video["poster_key"]),
            )
            for video in videos
        ],
    )


class Suspension(BaseModel):
    suspended: bool
    reason: str | None = Field(default=None, max_length=300)


@router.post("/users/{user_id}/suspension", response_model=UserOut)
async def set_suspension(
    user_id: UUID, body: Suspension, admin: AdminId, db: DbConnection
) -> UserOut:
    """Blocks sign-in without deleting anything. Their clips stay hidden while suspended."""
    if user_id == admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't suspend your own account")
    updated = await db.fetchval(
        """update auth.users
              set banned_until = case when $2 then 'infinity'::timestamptz else null end
            where id = $1 returning id""",
        user_id,
        body.suspended,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # Suspended accounts shouldn't keep showing up in the feed.
    await db.execute(
        "update public.videos set share_to_feed = $2 where owner_id = $1",
        user_id,
        not body.suspended,
    )
    row = await db.fetchrow(
        f"""select {USER_COLUMNS} from public.profiles p
              left join auth.users u on u.id = p.id
              left join public.athlete_profiles ap on ap.profile_id = p.id
              left join public.coach_profiles cp on cp.profile_id = p.id
             where p.id = $1""",
        user_id,
    )
    return UserOut(**dict(row))


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID, admin: AdminId, db: DbConnection, settings: SettingsDep
) -> Response:
    """Removes the account and every file that belongs to it."""
    if user_id == admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't delete your own account")
    videos = await db.fetch(
        "select raw_key, playback_key, poster_key from public.videos where owner_id = $1", user_id
    )
    flyers = await db.fetch(
        "select flyer_key from public.trials where created_by = $1 and flyer_key is not null",
        user_id,
    )
    deleted = await db.fetchval("delete from auth.users where id = $1 returning id", user_id)
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    media = [key for v in videos for key in (v["playback_key"], v["poster_key"]) if key]
    media += [f["flyer_key"] for f in flyers]
    raw = [v["raw_key"] for v in videos if v["raw_key"]]
    if raw:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_raw, raw)
    if media:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_media, media)
        await cdn.purge([storage.public_url(key) for key in media], settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------------------------
# trials
# ---------------------------------------------------------------------------------------------


class AdminTrialOut(BaseModel):
    id: UUID
    title: str
    club_name: str
    sport: str
    age_group: str | None
    venue: str
    region: str | None
    trial_date: dt.date
    application_deadline: dt.date
    status: str
    applicants_count: int
    flyer_url: str | None
    created_at: dt.datetime
    coach_id: UUID
    coach_name: str | None
    coach_email: str | None
    coach_verification: str | None


TRIAL_COLUMNS = """
  t.id, t.title, t.club_name, t.sport, t.age_group, t.venue, t.region, t.trial_date,
  t.application_deadline, t.status::text as status, t.applicants_count, t.flyer_key, t.created_at,
  t.created_by as coach_id, p.full_name as coach_name, u.email as coach_email,
  cp.verification_status::text as coach_verification
"""


def _trial(row) -> AdminTrialOut:
    data = dict(row)
    return AdminTrialOut(**data, flyer_url=storage.public_url(data.pop("flyer_key")))


@router.get("/trials", response_model=list[AdminTrialOut])
async def list_trials(
    admin: AdminId,
    db: DbConnection,
    status_filter: Literal["open", "closed", "cancelled", "all"] = Query("all", alias="status"),
    search: str = "",
    limit: int = Query(PAGE, ge=1, le=200),
) -> list[AdminTrialOut]:
    rows = await db.fetch(
        f"""select {TRIAL_COLUMNS}
              from public.trials t
              join public.profiles p on p.id = t.created_by
              left join auth.users u on u.id = t.created_by
              left join public.coach_profiles cp on cp.profile_id = t.created_by
             where ($1 = 'all' or t.status::text = $1)
               and ($2 = '' or t.title ilike '%' || $2 || '%' or t.club_name ilike '%' || $2 || '%'
                    or t.venue ilike '%' || $2 || '%')
             order by t.created_at desc
             limit $3""",
        status_filter,
        search.strip(),
        limit,
    )
    return [_trial(row) for row in rows]


class ApplicantOut(BaseModel):
    id: UUID
    athlete_id: UUID
    full_name: str | None
    age_group: str | None
    position: str | None
    status: str
    message: str | None
    created_at: dt.datetime


@router.get("/trials/{trial_id}/applicants", response_model=list[ApplicantOut])
async def list_applicants(trial_id: UUID, admin: AdminId, db: DbConnection) -> list[ApplicantOut]:
    rows = await db.fetch(
        """select a.id, a.athlete_id, p.full_name, ap.age_group, ap.position,
                  a.status::text as status, a.message, a.created_at
             from public.trial_applications a
             join public.profiles p on p.id = a.athlete_id
             left join public.athlete_profiles ap on ap.profile_id = a.athlete_id
            where a.trial_id = $1
            order by a.created_at desc""",
        trial_id,
    )
    return [ApplicantOut(**dict(row)) for row in rows]


class TrialAction(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/trials/{trial_id}/cancel", response_model=AdminTrialOut)
async def cancel_trial(
    trial_id: UUID, body: TrialAction, admin: AdminId, db: DbConnection
) -> AdminTrialOut:
    """Takes a trial down. Applicants see it as cancelled."""
    async with db.transaction():
        updated = await db.fetchval(
            "update public.trials set status = 'cancelled' where id = $1 returning id", trial_id
        )
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trial not found")
        await db.execute(
            """update public.trial_applications set status = 'rejected', decided_at = now(),
                      decided_by = $2
                where trial_id = $1 and status in ('pending', 'accepted')""",
            trial_id,
            admin,
        )
    row = await db.fetchrow(
        f"""select {TRIAL_COLUMNS}
              from public.trials t
              join public.profiles p on p.id = t.created_by
              left join auth.users u on u.id = t.created_by
              left join public.coach_profiles cp on cp.profile_id = t.created_by
             where t.id = $1""",
        trial_id,
    )
    return _trial(row)


# ---------------------------------------------------------------------------------------------
# clips
# ---------------------------------------------------------------------------------------------


class AdminVideoOut(VideoOut):
    owner_id: UUID
    owner_name: str | None
    reject_reason: str | None


@router.get("/videos", response_model=list[AdminVideoOut])
async def list_videos(
    admin: AdminId,
    db: DbConnection,
    status_filter: Literal["ready", "processing", "rejected", "failed", "all"] = Query(
        "ready", alias="status"
    ),
    search: str = "",
    limit: int = Query(PAGE, ge=1, le=200),
) -> list[AdminVideoOut]:
    rows = await db.fetch(
        """select v.id, v.title, v.status::text as status,
                  v.analysis_status::text as analysis_status,
                  v.rating, v.is_draft, v.share_to_feed, v.views, v.likes, v.playback_key,
                  v.poster_key, v.created_at, v.owner_id, p.full_name as owner_name,
                  v.reject_reason
             from public.videos v
             join public.profiles p on p.id = v.owner_id
            where ($1 = 'all'
                   or ($1 = 'processing' and v.status in ('uploading', 'processing'))
                   or v.status::text = $1)
              and ($2 = '' or v.title ilike '%' || $2 || '%' or p.full_name ilike '%' || $2 || '%')
            order by v.created_at desc
            limit $3""",
        status_filter,
        search.strip(),
        limit,
    )
    return [
        AdminVideoOut(
            **{k: v for k, v in dict(row).items() if k not in MEDIA_KEYS},
            playback_url=storage.public_url(row["playback_key"]),
            poster_url=storage.public_url(row["poster_key"]),
        )
        for row in rows
    ]


class VideoAction(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/videos/{video_id}/takedown", status_code=status.HTTP_204_NO_CONTENT)
async def takedown_video(
    video_id: UUID, body: VideoAction, admin: AdminId, db: DbConnection, settings: SettingsDep
) -> Response:
    """Removes a clip and its files; the athlete sees the reason on the clip."""
    row = await db.fetchrow(
        "select raw_key, playback_key, poster_key from public.videos where id = $1", video_id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")
    await db.execute(
        """update public.videos
              set status = 'rejected', reject_reason = $2, share_to_feed = false,
                  playback_key = null, poster_key = null, rating = null
            where id = $1""",
        video_id,
        body.reason,
    )
    media = [key for key in (row["playback_key"], row["poster_key"]) if key]
    if row["raw_key"]:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_raw, [row["raw_key"]])
    if media:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_media, media)
        await cdn.purge([storage.public_url(key) for key in media], settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------------------------


class ReportOut(BaseModel):
    id: UUID
    target_type: str
    target_id: UUID
    reason: str
    details: str | None
    status: str
    created_at: dt.datetime
    reporter_name: str | None
    reporter_id: UUID | None
    # what was reported, resolved for whichever kind it is
    subject: str | None
    subject_owner: str | None
    subject_owner_id: UUID | None
    playback_url: str | None
    poster_url: str | None
    reports_on_subject: int
    review_note: str | None
    reviewed_at: dt.datetime | None


REPORTS_QUERY = """
select r.id, r.target_type::text as target_type, r.target_id, r.reason, r.details,
       r.status::text as status, r.created_at, r.review_note, r.reviewed_at,
       r.reporter_id, reporter.full_name as reporter_name,
       case r.target_type
         when 'video' then v.title
         when 'trial' then t.title
         else subject_profile.full_name
       end as subject,
       case r.target_type
         when 'video' then owner.full_name
         when 'trial' then coach.full_name
         else subject_profile.full_name
       end as subject_owner,
       case r.target_type
         when 'video' then v.owner_id
         when 'trial' then t.created_by
         else subject_profile.id
       end as subject_owner_id,
       v.playback_key, v.poster_key,
       (select count(*) from public.reports other
         where other.target_type = r.target_type
           and other.target_id = r.target_id) as reports_on_subject
  from public.reports r
  left join public.profiles reporter on reporter.id = r.reporter_id
  left join public.videos v on r.target_type = 'video' and v.id = r.target_id
  left join public.profiles owner on owner.id = v.owner_id
  left join public.trials t on r.target_type = 'trial' and t.id = r.target_id
  left join public.profiles coach on coach.id = t.created_by
  left join public.profiles subject_profile
         on r.target_type = 'profile' and subject_profile.id = r.target_id
 where ($1::text is null or r.status::text = $1)
 order by r.status = 'open' desc, r.created_at desc
 limit $2
"""


@router.get("/reports", response_model=list[ReportOut])
async def list_reports(
    admin: AdminId,
    db: DbConnection,
    report_status: Annotated[
        Literal["open", "actioned", "dismissed"] | None, Query(alias="status")
    ] = None,
) -> list[ReportOut]:
    """The moderation queue: what people reported, open first."""
    rows = await db.fetch(REPORTS_QUERY, report_status, PAGE)
    return [
        ReportOut(
            **{k: v for k, v in dict(row).items() if k not in MEDIA_KEYS},
            playback_url=storage.public_url(row["playback_key"]),
            poster_url=storage.public_url(row["poster_key"]),
        )
        for row in rows
    ]


class ReportDecision(BaseModel):
    status: Literal["actioned", "dismissed"]
    note: str | None = Field(default=None, max_length=300)


@router.post("/reports/{report_id}/decision", response_model=ReportOut)
async def decide_report(
    report_id: UUID, body: ReportDecision, admin: AdminId, db: DbConnection
) -> ReportOut:
    """Closes a report. Acting on the content itself (take-down, suspension) is separate."""
    updated = await db.fetchval(
        """update public.reports
              set status = $2::public.report_status, review_note = $3,
                  reviewed_by = $4::uuid, reviewed_at = now()
            where id = $1
        returning id""",
        report_id,
        body.status,
        body.note,
        admin,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    one = REPORTS_QUERY.replace(
        "where ($1::text is null or r.status::text = $1)", "where r.id = $1::uuid"
    )
    row = await db.fetchrow(one, str(report_id), 1)
    return ReportOut(
        **{k: v for k, v in dict(row).items() if k not in MEDIA_KEYS},
        playback_url=storage.public_url(row["playback_key"]),
        poster_url=storage.public_url(row["poster_key"]),
    )
