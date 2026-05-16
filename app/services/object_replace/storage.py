from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import uuid

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger

from app.core.config import settings
from app.services.object_replace.schemas import SupportedImageContentType

CONTENT_TYPE_EXTENSION: dict[SupportedImageContentType, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class ObjectReplaceStorageError(RuntimeError):
    """Raised when object storage cannot satisfy an Object Replace operation."""


@dataclass(frozen=True)
class PresignedObjectReplaceUpload:
    upload_id: uuid.UUID
    object_key: str
    upload_url: str
    original_image_url: str
    headers: dict[str, str]
    expires_in: int


def _require_storage_settings() -> None:
    missing = [
        name
        for name, value in (
            ("R2_ENDPOINT_URL", settings.r2_endpoint_url),
            ("R2_BUCKET_NAME", settings.r2_bucket_name),
            ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
        )
        if not value
    ]
    if missing:
        raise ObjectReplaceStorageError(
            f"Object storage is not configured: missing {', '.join(missing)}"
        )


def _get_s3_client():
    _require_storage_settings()

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=BotoConfig(
            region_name=settings.r2_region,
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.s3_force_path_style else "virtual"},
        ),
    )


def sanitize_path_part(value: str, *, fallback: str = "image", max_length: int = 120) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-_")
    return (sanitized or fallback)[:max_length]


def build_object_replace_key(
    *,
    user_id: uuid.UUID | str,
    upload_id: uuid.UUID,
    file_name: str,
    content_type: SupportedImageContentType,
) -> str:
    extension = CONTENT_TYPE_EXTENSION[content_type]
    safe_user_id = sanitize_path_part(str(user_id), fallback="user")
    safe_stem = sanitize_path_part(PurePosixPath(file_name).stem, fallback="image")
    return f"object-replace/{safe_user_id}/{upload_id}-{safe_stem}{extension}"


def _encode_object_key_for_url(key: str) -> str:
    from urllib.parse import quote

    return "/".join(quote(part, safe="") for part in key.split("/"))


def _build_public_url(key: str) -> str | None:
    if not settings.r2_public_url:
        return None
    return f"{settings.r2_public_url.rstrip('/')}/{_encode_object_key_for_url(key)}"


def create_presigned_upload(
    *,
    user_id: uuid.UUID,
    file_name: str,
    content_type: SupportedImageContentType,
    size_bytes: int | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
) -> PresignedObjectReplaceUpload:
    upload_id = uuid.uuid4()
    object_key = build_object_replace_key(
        user_id=user_id,
        upload_id=upload_id,
        file_name=file_name,
        content_type=content_type,
    )
    # Keep required request headers minimal for mobile clients. React Native/iOS may
    # normalize or omit custom x-amz-meta headers, which can invalidate a strict
    # signature and cause 401/403 Unauthorized responses from object storage.
    headers = {
        "Content-Type": content_type,
    }

    try:
        client = _get_s3_client()
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.r2_bucket_name,
                "Key": object_key,
            },
            ExpiresIn=settings.r2_presigned_url_expiry,
        )

        original_image_url = _build_public_url(object_key) or client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.r2_bucket_name,
                "Key": object_key,
            },
            ExpiresIn=settings.r2_download_url_expiry,
        )
    except (BotoCoreError, ClientError, ObjectReplaceStorageError) as exc:
        logger.exception("Failed to create Object Replace upload URL | key={}", object_key)
        raise ObjectReplaceStorageError("Could not create upload URL") from exc

    return PresignedObjectReplaceUpload(
        upload_id=upload_id,
        object_key=object_key,
        upload_url=upload_url,
        original_image_url=original_image_url,
        headers=headers,
        expires_in=settings.r2_presigned_url_expiry,
    )


async def create_presigned_upload_async(
    *,
    user_id: uuid.UUID,
    file_name: str,
    content_type: SupportedImageContentType,
    size_bytes: int | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
) -> PresignedObjectReplaceUpload:
    return await asyncio.to_thread(
        create_presigned_upload,
        user_id=user_id,
        file_name=file_name,
        content_type=content_type,
        size_bytes=size_bytes,
        image_width=image_width,
        image_height=image_height,
    )


def object_exists(object_key: str) -> bool:
    try:
        client = _get_s3_client()
        client.head_object(Bucket=settings.r2_bucket_name, Key=object_key)
        return True
    except ClientError:
        return False
    except BotoCoreError as exc:
        raise ObjectReplaceStorageError("Could not verify uploaded image") from exc


async def object_exists_async(object_key: str) -> bool:
    return await asyncio.to_thread(object_exists, object_key)
