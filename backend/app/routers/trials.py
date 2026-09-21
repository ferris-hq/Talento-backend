"""Trials posted by verified coaches. Each trial can carry one flyer image (never a video).

Flyer flow:
  1. POST /v1/trials/flyer-uploads     -> presigned PUT URL for the raw image
  2. client PUTs the image to R2
  3. POST /v1/trials (or PATCH) with flyer_upload_id -> the server re-encodes it into the media
     bucket and removes the raw upload
Applying/withdrawing happens through Postgres RPCs (apply_to_trial, withdraw_application).
"""

import datetime as dt
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Settings, get_settings
from app.db import DbConnection
from app.deps.auth import CurrentUser
from app.deps.limits import TrialLimit
from app.services import cdn, images, storage

router = APIRouter(prefix="/v1/trials", tags=["trials"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
FLYER_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FLYER_BYTES = 10 * 1024 * 1024
SPORTS = {"football", "boxing", "athletics", "basketball", "hockey", "others"}
AgeGroup = Literal["U-13", "U-15", "U-17", "U-19", "U-21", "Senior"]
SkillLevel = Literal["Beginner", "Intermediate", "Advanced"]

TRIAL_COLUMNS = """id, created_by, club_name, title, sport, position, age_group, skill_level,
  description, requirements, venue, region, trial_date, start_time, application_deadline,
  capacity, flyer_key, flyer_width, flyer_height, status::text as status, applicants_count,
  created_at"""


class FlyerUploadRequest(BaseModel):
    content_type: str
    size_bytes: int = Field(gt=0)


class FlyerUploadResponse(BaseModel):
    upload_id: UUID
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


class TrialFields(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    sport: str
    position: str | None = Field(default=None, max_length=60)
    age_group: AgeGroup | None = None
    skill_level: SkillLevel | None = None
    description: str = Field(default="", max_length=1500)
    requirements: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=10
    )
    venue: str = Field(min_length=2, max_length=120)
    region: str | None = Field(default=None, max_length=60)
    trial_date: dt.date
    start_time: dt.time | None = None
    application_deadline: dt.date
    capacity: int | None = Field(default=None, ge=1, le=10_000)

    @field_validator("sport")
    @classmethod
    def known_sport(cls, value: str) -> str:
        if value not in SPORTS:
            raise ValueError("Unknown sport")
        return value

    @field_validator("title", "venue", "description")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("requirements")
    @classmethod
    def strip_items(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @model_validator(mode="after")
    def dates_make_sense(self) -> "TrialFields":
        today = dt.date.today()
        if self.application_deadline < today:
            raise ValueError("The application deadline can't be in the past")
        if self.application_deadline > self.trial_date:
            raise ValueError("Applications must close on or before the trial day")
        return self


class TrialCreate(TrialFields):
    club_name: str | None = Field(default=None, max_length=80)
    flyer_upload_id: UUID | None = None


class TrialUpdate(BaseModel):
    """Partial edit. Status can move between open and closed; cancel with DELETE."""

    status: Literal["open", "closed"] | None = None
    fields: TrialFields | None = None
    flyer_upload_id: UUID | None = None
    remove_flyer: bool = False


class TrialOut(BaseModel):
    id: UUID
    club_name: str
    title: str
    sport: str
    position: str | None
    age_group: str | None
    skill_level: str | None
    description: str
    requirements: list[str]
    venue: str
    region: str | None
    trial_date: dt.date
    start_time: dt.time | None
    application_deadline: dt.date
    capacity: int | None
    flyer_url: str | None
    flyer_width: int | None
    flyer_height: int | None
    status: str
    applicants_count: int
    created_at: dt.datetime


def _to_out(row) -> TrialOut:
    data = dict(row)
    data["flyer_url"] = storage.public_url(data.pop("flyer_key"))
    data.pop("created_by", None)
    return TrialOut(**data)


async def _require_verified_coach(db, user_id: UUID) -> str | None:
    row = await db.fetchrow(
        """select p.role::text as role, cp.verification_status::text as verification,
                  cp.organization_text
             from public.profiles p
             left join public.coach_profiles cp on cp.profile_id = p.id
            where p.id = $1""",
        user_id,
    )
    if row is None or row["role"] not in {"coach", "admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only coaches can post trials")
    if row["role"] == "coach" and row["verification"] != "verified":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Your coach account must be verified before you post trials"
        )
    return row["organization_text"]


async def _own_trial(db, trial_id: UUID, user_id: UUID):
    row = await db.fetchrow(
        f"select {TRIAL_COLUMNS} from public.trials where id = $1 and created_by = $2",
        trial_id,
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trial not found")
    return row


def _store_flyer(settings: Settings, owner_id: str, upload_id: str, trial_id: str):
    """Re-encode an uploaded flyer into the media bucket. Returns (key, width, height)."""
    src_key = storage.flyer_upload_key(owner_id, upload_id)
    size = storage.object_size(settings.r2_bucket_raw, src_key)
    if size is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The flyer upload hasn't finished yet")
    try:
        if size > MAX_FLYER_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Flyers must be under 10 MB"
            )
        with tempfile.TemporaryDirectory(prefix="talento-flyer-") as tmp:
            path = Path(tmp) / "flyer"
            storage.download_file(settings.r2_bucket_raw, src_key, str(path))
            try:
                data, width, height = images.process_flyer(path)
            except images.UnusableImage as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        # A new name per upload, so the immutable CDN cache never serves an old flyer.
        key = f"t/{trial_id}/flyer-{uuid4().hex[:12]}.jpg"
        storage.put_bytes(settings.r2_bucket_media, key, data, "image/jpeg")
        return key, width, height
    finally:
        storage.delete_objects(settings.r2_bucket_raw, [src_key])


async def _drop_flyer(settings: Settings, key: str | None) -> None:
    if not key:
        return
    await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_media, [key])
    await cdn.purge([storage.public_url(key)], settings)


@router.post(
    "/flyer-uploads", response_model=FlyerUploadResponse, status_code=status.HTTP_201_CREATED
)
async def create_flyer_upload(
    body: FlyerUploadRequest, user: CurrentUser, db: DbConnection, settings: SettingsDep
) -> FlyerUploadResponse:
    await _require_verified_coach(db, user.id)
    content_type = body.content_type.lower()
    if content_type not in FLYER_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Flyers must be images (JPG, PNG or WebP). Videos aren't supported for trials.",
        )
    if body.size_bytes > MAX_FLYER_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Flyers must be under 10 MB")
    upload_id = uuid4()
    key = storage.flyer_upload_key(str(user.id), str(upload_id))
    url = await to_thread.run_sync(storage.presigned_put, settings.r2_bucket_raw, key, content_type)
    return FlyerUploadResponse(
        upload_id=upload_id, upload_url=url, headers={"Content-Type": content_type}
    )


@router.post("", response_model=TrialOut, status_code=status.HTTP_201_CREATED)
async def create_trial(
    body: TrialCreate,
    user: CurrentUser,
    db: DbConnection,
    settings: SettingsDep,
    _limit: TrialLimit,
) -> TrialOut:
    organization = await _require_verified_coach(db, user.id)
    club_name = _clean(organization) or _clean(body.club_name)
    if not club_name or len(club_name) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Add your club or academy name first"
        )

    trial_id = uuid4()
    flyer = (None, None, None)
    if body.flyer_upload_id:
        flyer = await to_thread.run_sync(
            _store_flyer, settings, str(user.id), str(body.flyer_upload_id), str(trial_id)
        )
    try:
        row = await db.fetchrow(
            f"""insert into public.trials
                  (id, created_by, club_name, title, sport, position, age_group, skill_level,
                   description, requirements, venue, region, trial_date, start_time,
                   application_deadline, capacity, flyer_key, flyer_width, flyer_height)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                        $17, $18, $19)
                returning {TRIAL_COLUMNS}""",
            trial_id,
            user.id,
            club_name,
            body.title,
            body.sport,
            _clean(body.position),
            body.age_group,
            body.skill_level,
            body.description,
            body.requirements,
            body.venue,
            _clean(body.region),
            body.trial_date,
            body.start_time,
            body.application_deadline,
            body.capacity,
            *flyer,
        )
    except Exception:
        await _drop_flyer(settings, flyer[0])
        raise
    return _to_out(row)


@router.patch("/{trial_id}", response_model=TrialOut)
async def update_trial(
    trial_id: UUID,
    body: TrialUpdate,
    user: CurrentUser,
    db: DbConnection,
    settings: SettingsDep,
) -> TrialOut:
    await _require_verified_coach(db, user.id)
    row = await _own_trial(db, trial_id, user.id)
    if row["status"] == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "This trial was cancelled")

    updates: dict[str, object] = {}
    if body.status:
        updates["status"] = body.status
    if body.fields:
        f = body.fields
        updates.update(
            title=f.title,
            sport=f.sport,
            position=_clean(f.position),
            age_group=f.age_group,
            skill_level=f.skill_level,
            description=f.description,
            requirements=f.requirements,
            venue=f.venue,
            region=_clean(f.region),
            trial_date=f.trial_date,
            start_time=f.start_time,
            application_deadline=f.application_deadline,
            capacity=f.capacity,
        )
    old_flyer = row["flyer_key"]
    new_flyer = None
    if body.flyer_upload_id:
        new_flyer = await to_thread.run_sync(
            _store_flyer, settings, str(user.id), str(body.flyer_upload_id), str(trial_id)
        )
        updates.update(flyer_key=new_flyer[0], flyer_width=new_flyer[1], flyer_height=new_flyer[2])
    elif body.remove_flyer:
        updates.update(flyer_key=None, flyer_width=None, flyer_height=None)
    if not updates:
        return _to_out(row)

    # Column names come from the fixed keys above; values are always parameters.
    assignments = ", ".join(
        f"{column} = ${i}{'::public.trial_status' if column == 'status' else ''}"
        for i, column in enumerate(updates, start=2)
    )
    try:
        updated = await db.fetchrow(
            f"update public.trials set {assignments} where id = $1 returning {TRIAL_COLUMNS}",
            trial_id,
            *updates.values(),
        )
    except Exception:
        if new_flyer:
            await _drop_flyer(settings, new_flyer[0])
        raise
    if "flyer_key" in updates and old_flyer:
        await _drop_flyer(settings, old_flyer)
    return _to_out(updated)


@router.delete("/{trial_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trial(
    trial_id: UUID, user: CurrentUser, db: DbConnection, settings: SettingsDep
) -> Response:
    """Deletes a trial nobody applied to; otherwise cancels it so applicants keep a record."""
    await _require_verified_coach(db, user.id)
    row = await _own_trial(db, trial_id, user.id)
    has_applications = await db.fetchval(
        "select exists (select 1 from public.trial_applications where trial_id = $1)", trial_id
    )
    if has_applications:
        await db.execute("update public.trials set status = 'cancelled' where id = $1", trial_id)
        await db.execute(
            """update public.trial_applications set status = 'rejected', decided_at = now()
                where trial_id = $1 and status in ('pending', 'accepted')""",
            trial_id,
        )
    else:
        await db.execute("delete from public.trials where id = $1", trial_id)
        await _drop_flyer(settings, row["flyer_key"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
