from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import fal_client
from loguru import logger

from app.core.config import settings
from app.services.design_generation.models import (
    DesignGenerationError,
    DesignGenerationResult,
)

GENERATE_TIMEOUT = settings.fal_timeout_minutes * 60


class FalGenerationError(DesignGenerationError):
    """Raised when the fal.ai provider cannot complete generation."""


def _parse_result(output: dict[str, Any]) -> DesignGenerationResult:
    images = output.get("images")
    if not isinstance(images, list) or not images:
        keys = list(output.keys())
        raise FalGenerationError(
            f"Invalid fal.ai response payload: missing non-empty 'images' | keys={keys}"
        )

    first = images[0]
    if not isinstance(first, dict):
        raise FalGenerationError("Invalid fal.ai response payload: images[0] is not an object")

    url = first.get("url")
    if not isinstance(url, str) or not url:
        raise FalGenerationError("Invalid fal.ai response payload: images[0].url is missing")

    content_type = first.get("content_type")
    media_type = "image"
    if isinstance(content_type, str) and "/" in content_type:
        media_type = content_type.split("/", 1)[0]

    request_id = output.get("request_id")
    job_id = request_id if isinstance(request_id, str) else None

    return DesignGenerationResult(url=url, media_type=media_type, job_id=job_id)


async def _upload_input_image(image_path: str) -> str:
    local_path = Path(image_path)
    if not local_path.exists():
        raise FalGenerationError(f"Input image not found on disk: {local_path}")

    try:
        # Upload is intentionally offloaded to a thread per integration requirement.
        return await asyncio.to_thread(fal_client.upload_file, local_path)
    except Exception as exc:
        raise FalGenerationError(f"fal.ai upload failed: {exc}") from exc


async def generate_image(
    *,
    model: str,
    prompt: str,
    image_path: str,
    aspect_ratio: str = "1:1",
    resolution: str = "1K",
    output_format: str = "png",
    timeout: int = GENERATE_TIMEOUT,
) -> DesignGenerationResult:
    if not settings.fal_key:
        raise FalGenerationError("FAL_KEY is required when fal.ai provider is enabled")

    image_url = await _upload_input_image(image_path)

    client = fal_client.AsyncClient(
        key=settings.fal_key,
        default_timeout=float(timeout),
    )

    arguments = {
        "prompt": prompt,
        "image_urls": [image_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
    }

    try:
        logger.info("fal.ai request | model={} | timeout_s={}", model, timeout)
        output = await client.subscribe(
            model,
            arguments=arguments,
            client_timeout=timeout,
        )
    except Exception as exc:
        raise FalGenerationError(f"fal.ai generation request failed: {exc}") from exc

    if not isinstance(output, dict):
        raise FalGenerationError(
            f"Invalid fal.ai response type: expected object, got {type(output).__name__}"
        )

    return _parse_result(output)
