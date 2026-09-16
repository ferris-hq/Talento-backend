"""Video uploads.

Flow:
  1. POST /v1/uploads                   -> creates the row (status uploading) + presigned PUT URL
  2. client PUTs the file to R2
  3. POST /v1/uploads/{id}/complete     -> verifies the object, status processing, queues the worker
  4. worker transcodes                  -> status ready (or rejected/failed)
Drafts use the same flow with `draft: true`; POST /v1/videos/{id}/publish makes them public.
"""

from typing import Annotated, Literal
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import DbConnection
from app.deps.auth import CurrentUser
from app.services import cdn, jobs, storage

router = APIRouter(prefix="/v1", tags=["videos"])

ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/x-m4v", "video/3gpp", "video/webm"}
Category = Literal["Skills", "Training", "Match", "Passing"]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class UploadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    caption: str = Field(default="", max_length=150)
    category: Category = "Skills"
    share_to_feed: bool = True
    draft: bool = False
    content_type: str = "video/mp4"
    size_bytes: int = Field(gt=0)
    duration_s: float | None = Field(default=None, ge=0)


class UploadResponse(BaseModel):
    video_id: UUID
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


class CompleteRequest(BaseModel):
    draft: bool | None = None


class VideoOut(BaseModel):
    id: UUID
    title: str
    caption: str
    category: Category
    status: str
    is_draft: bool
    share_to_feed: bool
    reject_reason: str | None
    duration_s: float | None
    width: int | None
    height: int | None
    rating: float | None
    views: int
    playback_url: str | None
    poster_url: str | None
    created_at: str


VIDEO_COLUMNS = """id, owner_id, title, caption, category::text as category, status::text as status,
  is_draft, share_to_feed, reject_reason, duration_s, width, height, rating, views,
  raw_key, playback_key, poster_key, created_at"""


def _to_out(row) -> VideoOut:
    return VideoOut(
        id=row["id"],
        title=row["title"],
        caption=row["caption"],
        category=row["category"],
        status=row["status"],
        is_draft=row["is_draft"],
        share_to_feed=row["share_to_feed"],
        reject_reason=row["reject_reason"],
        duration_s=float(row["duration_s"]) if row["duration_s"] is not None else None,
        width=row["width"],
        height=row["height"],
        rating=float(row["rating"]) if row["rating"] is not None else None,
        views=row["views"],
        playback_url=storage.public_url(row["playback_key"]),
        poster_url=storage.public_url(row["poster_key"]),
        created_at=row["created_at"].isoformat(),
    )


async def _own_video(db, video_id: UUID, user_id: UUID):
    row = await db.fetchrow(
        f"select {VIDEO_COLUMNS} from public.videos where id = $1 and owner_id = $2",
        video_id,
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")
    return row


async def _require_athlete(db, user_id: UUID) -> str | None:
    row = await db.fetchrow(
        "select role::text as role, sport from public.profiles where id = $1", user_id
    )
    if row is None or row["role"] != "athlete":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only athletes can upload videos")
    return row["sport"]


async def _check_slots(db, user_id: UUID, settings: Settings) -> None:
    used = await db.fetchval("select public.video_slots_used($1)", user_id)
    if used >= settings.max_library_videos:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Your library is full ({settings.max_library_videos} videos). "
            "Remove one to upload another.",
        )


@router.post("/uploads", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: UploadRequest, user: CurrentUser, db: DbConnection, settings: SettingsDep
) -> UploadResponse:
    sport = await _require_athlete(db, user.id)
    content_type = body.content_type.lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload an MP4 or MOV video")
    if body.size_bytes > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Videos must be under {limit_mb} MB"
        )
    if body.duration_s is not None and body.duration_s > settings.max_video_seconds + 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Clips can be at most {settings.max_video_seconds} seconds",
        )
    if not body.draft:
        await _check_slots(db, user.id, settings)

    video_id = await db.fetchval(
        """insert into public.videos
             (owner_id, title, caption, category, sport, is_draft, share_to_feed,
              content_type, size_bytes, duration_s, status)
           values ($1, $2, $3, $4::public.video_category, $5, $6, $7, $8, $9, $10, 'uploading')
           returning id""",
        user.id,
        body.title.strip(),
        body.caption.strip(),
        body.category,
        sport,
        body.draft,
        body.share_to_feed,
        content_type,
        body.size_bytes,
        body.duration_s,
    )
    key = storage.raw_key(str(user.id), str(video_id))
    await db.execute("update public.videos set raw_key = $2 where id = $1", video_id, key)
    url = await to_thread.run_sync(storage.presigned_put, settings.r2_bucket_raw, key, content_type)
    return UploadResponse(video_id=video_id, upload_url=url, headers={"Content-Type": content_type})


@router.post("/uploads/{video_id}/complete", response_model=VideoOut)
async def complete_upload(
    video_id: UUID,
    body: CompleteRequest,
    request: Request,
    user: CurrentUser,
    db: DbConnection,
    settings: SettingsDep,
) -> VideoOut:
    row = await _own_video(db, video_id, user.id)
    if row["status"] != "uploading":
        return _to_out(row)  # already completed: idempotent

    size = await to_thread.run_sync(storage.object_size, settings.r2_bucket_raw, row["raw_key"])
    if size is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The upload hasn't finished yet")
    if size > settings.max_upload_bytes:
        await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_raw, [row["raw_key"]])
        await db.execute(
            "update public.videos set status = 'rejected', reject_reason = $2 where id = $1",
            video_id,
            "The file is larger than allowed.",
        )
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than allowed"
        )

    is_draft = row["is_draft"] if body.draft is None else body.draft
    if not is_draft and row["is_draft"]:
        await _check_slots(db, user.id, settings)

    updated = await db.fetchrow(
        f"""update public.videos
               set status = 'processing', is_draft = $2, size_bytes = $3,
                   processing_started_at = now()
             where id = $1
         returning {VIDEO_COLUMNS}""",
        video_id,
        is_draft,
        size,
    )
    try:
        await jobs.enqueue_process_video(request.app.state.queue, str(video_id))
    except Exception:
        # Let the client retry "complete" instead of leaving the clip stuck in processing.
        await db.execute(
            "update public.videos set status = 'uploading', processing_started_at = null"
            " where id = $1",
            video_id,
        )
        raise
    return _to_out(updated)


@router.post("/videos/{video_id}/publish", response_model=VideoOut)
async def publish_video(
    video_id: UUID, user: CurrentUser, db: DbConnection, settings: SettingsDep
) -> VideoOut:
    row = await _own_video(db, video_id, user.id)
    if not row["is_draft"]:
        return _to_out(row)
    if row["status"] in {"rejected", "failed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This clip couldn't be processed. Upload it again."
        )
    await _check_slots(db, user.id, settings)
    updated = await db.fetchrow(
        f"update public.videos set is_draft = false where id = $1 returning {VIDEO_COLUMNS}",
        video_id,
    )
    return _to_out(updated)


@router.get("/videos", response_model=list[VideoOut])
async def list_my_videos(user: CurrentUser, db: DbConnection) -> list[VideoOut]:
    rows = await db.fetch(
        f"""select {VIDEO_COLUMNS} from public.videos
             where owner_id = $1 and status <> 'uploading'
             order by created_at desc""",
        user.id,
    )
    return [_to_out(r) for r in rows]


@router.delete("/videos/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_video(
    video_id: UUID, user: CurrentUser, db: DbConnection, settings: SettingsDep
) -> Response:
    row = await _own_video(db, video_id, user.id)
    await to_thread.run_sync(storage.delete_objects, settings.r2_bucket_raw, [row["raw_key"]])
    await to_thread.run_sync(
        storage.delete_objects, settings.r2_bucket_media, [row["playback_key"], row["poster_key"]]
    )
    await db.execute("delete from public.videos where id = $1", video_id)
    # Otherwise the edge keeps serving the deleted clip until its cache expires.
    await cdn.purge(
        [storage.public_url(row["playback_key"]), storage.public_url(row["poster_key"])], settings
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
