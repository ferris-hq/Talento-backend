"""Cloudflare R2 (S3 API) access. boto3 blocks: call it from a worker thread in async code."""

from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import Settings, get_settings

UPLOAD_URL_TTL = 60 * 60  # seconds
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class StorageNotConfigured(RuntimeError):
    pass


@lru_cache
def _client(endpoint: str, key_id: str, secret: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )


def client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    if not (s.r2_endpoint and s.r2_access_key_id and s.r2_secret_access_key):
        raise StorageNotConfigured("R2 credentials are not configured")
    return _client(s.r2_endpoint, s.r2_access_key_id, s.r2_secret_access_key)


def raw_key(owner_id: str, video_id: str) -> str:
    return f"raw/{owner_id}/{video_id}"


def media_keys(video_id: str) -> tuple[str, str]:
    return f"v/{video_id}/720.mp4", f"v/{video_id}/poster.jpg"


def presigned_put(bucket: str, key: str, content_type: str) -> str:
    return client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_TTL,
    )


def object_size(bucket: str, key: str) -> int | None:
    """Size in bytes, or None if the object doesn't exist."""
    try:
        head = client().head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return int(head["ContentLength"])


def delete_objects(bucket: str, keys: list[str]) -> None:
    keys = [k for k in keys if k]
    if keys:
        client().delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True}
        )


def public_url(key: str | None, settings: Settings | None = None) -> str | None:
    if not key:
        return None
    base = (settings or get_settings()).r2_public_base_url.rstrip("/")
    return f"{base}/{key}"


def flyer_upload_key(owner_id: str, upload_id: str) -> str:
    return f"flyers/{owner_id}/{upload_id}"


def download_file(bucket: str, key: str, dest: str) -> None:
    client().download_file(bucket, key, dest)


def put_bytes(bucket: str, key: str, body: bytes, content_type: str) -> None:
    client().put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type, CacheControl=IMMUTABLE_CACHE
    )
