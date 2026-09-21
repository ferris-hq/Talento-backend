from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.config import Settings, get_settings
from app.db import DbConnection
from app.deps.auth import CurrentUser
from app.deps.limits import DeleteAccountLimit
from app.schemas.profile import AthleteSummary, CoachSummary, Me
from app.services import cdn, storage

router = APIRouter(prefix="/v1", tags=["me"])

SettingsDep = Annotated[Settings, Depends(get_settings)]

ME_QUERY = """
select p.id, p.role::text as role, p.full_name, p.avatar_key, p.region, p.city, p.sport,
       p.onboarded_at,
       ap.profile_id is not null as has_athlete,
       ap.position, ap.age_group, ap.is_minor, ap.guardian_consented,
       ap.visibility::text as visibility,
       cp.profile_id is not null as has_coach,
       cp.club_id, cp.organization_text, cp.focus_sport,
       cp.verification_status::text as verification_status
  from public.profiles p
  left join public.athlete_profiles ap on ap.profile_id = p.id
  left join public.coach_profiles cp on cp.profile_id = p.id
 where p.id = $1
"""


@router.get("/me", response_model=Me)
async def read_me(user: CurrentUser, db: DbConnection) -> Me:
    """The signed-in user's profile, with athlete or coach details when set up."""
    row = await db.fetchrow(ME_QUERY, user.id)
    if row is None:
        # The signup trigger creates profiles; a missing row means the auth user was deleted.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")

    athlete = (
        AthleteSummary(
            position=row["position"],
            age_group=row["age_group"],
            is_minor=row["is_minor"],
            guardian_consented=row["guardian_consented"],
            visibility=row["visibility"],
        )
        if row["has_athlete"]
        else None
    )
    coach = (
        CoachSummary(
            club_id=row["club_id"],
            organization_text=row["organization_text"],
            focus_sport=row["focus_sport"],
            verification_status=row["verification_status"],
        )
        if row["has_coach"]
        else None
    )

    return Me(
        id=row["id"],
        email=user.email,
        phone=user.phone,
        role=row["role"],
        full_name=row["full_name"],
        avatar_key=row["avatar_key"],
        region=row["region"],
        city=row["city"],
        sport=row["sport"],
        onboarded_at=row["onboarded_at"],
        athlete=athlete,
        coach=coach,
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    user: CurrentUser, db: DbConnection, settings: SettingsDep, _limit: DeleteAccountLimit
) -> Response:
    """Deletes the signed-in account and every file that belongs to it. There is no undo."""
    videos = await db.fetch(
        "select raw_key, playback_key, poster_key from public.videos where owner_id = $1", user.id
    )
    flyers = await db.fetch(
        "select flyer_key from public.trials where created_by = $1 and flyer_key is not null",
        user.id,
    )
    # Everything the account owns hangs off auth.users by "on delete cascade".
    deleted = await db.fetchval("delete from auth.users where id = $1 returning id", user.id)
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    media = [key for v in videos for key in (v["playback_key"], v["poster_key"]) if key]
    media += [f["flyer_key"] for f in flyers]
    raw = [v["raw_key"] for v in videos if v["raw_key"]]
    if raw:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_raw, raw)
    if media:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_media, media)
        await cdn.purge([storage.public_url(key) for key in media], settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
