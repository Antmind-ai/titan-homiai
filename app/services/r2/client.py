from __future__ import annotations

import asyncio
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from loguru import logger

from app.core.config import settings


def _get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=BotoConfig(
            signature_version="s3v4",
            region_name="auto",
        ),
    )


def upload_file(key: str, file_data: bytes, content_type: str) -> None:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=file_data,
        ContentType=content_type,
    )
    logger.debug("R2 upload success | key={} | size={}", key, len(file_data))


async def upload_file_async(key: str, file_data: bytes, content_type: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, upload_file, key, file_data, content_type)


def download_to_path(key: str, local_path: Path) -> None:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(str(settings.r2_bucket_name), key, str(local_path))
    logger.debug("R2 download success | key={} | path={}", key, local_path)


async def download_to_path_async(key: str, local_path: Path) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, download_to_path, key, local_path)


def delete_object(key: str) -> bool:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    try:
        client.head_object(Bucket=settings.r2_bucket_name, Key=key)
    except ClientError:
        return False

    client.delete_object(Bucket=settings.r2_bucket_name, Key=key)
    logger.debug("R2 delete success | key={}", key)
    return True


async def delete_object_async(key: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, delete_object, key)


def list_objects_with_prefix(prefix: str) -> list[str]:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    keys: list[str] = []
    continuation_token: str | None = None

    while True:
        params: dict[str, str] = {
            "Bucket": settings.r2_bucket_name,
            "Prefix": prefix,
        }
        if continuation_token:
            params["ContinuationToken"] = continuation_token

        response = client.list_objects_v2(**params)
        contents = response.get("Contents", [])
        for item in contents:
            key = item.get("Key")
            if isinstance(key, str):
                keys.append(key)

        if not response.get("IsTruncated"):
            break

        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break

    return keys


async def list_objects_with_prefix_async(prefix: str) -> list[str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, list_objects_with_prefix, prefix)


def object_exists(key: str) -> bool:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    try:
        client.head_object(Bucket=settings.r2_bucket_name, Key=key)
        return True
    except ClientError:
        return False


def generate_presigned_url(key: str, expiry: int | None = None) -> str:
    if not settings.r2_bucket_name:
        raise RuntimeError("R2 bucket name is not configured")

    client = _get_r2_client()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.r2_bucket_name,
            "Key": key,
        },
        ExpiresIn=expiry or settings.r2_presigned_url_expiry,
    )


def build_r2_key(user_id: str, filename: str) -> str:
    return f"{user_id}/{filename}"
