from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import struct
from typing import Any
import zlib

import fal_client
from loguru import logger

from app.core.config import settings
from app.services.object_replace.schemas import ObjectReplacePoint


class ObjectReplaceFalError(RuntimeError):
    """Raised when fal.ai cannot complete the Object Replace pipeline."""


def _require_fal_key() -> None:
    if not settings.fal_key:
        raise ObjectReplaceFalError("FAL_KEY is required for Object Replace")
    os.environ["FAL_KEY"] = settings.fal_key


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def build_circular_mask_png(
    *,
    width: int,
    height: int,
    point: ObjectReplacePoint,
    radius: int | None = None,
) -> bytes:
    if width < 1 or height < 1:
        raise ValueError("Mask dimensions must be positive")

    radius_px = radius if radius is not None else max(28, int(min(width, height) * 0.08))
    radius_px = max(8, min(radius_px, max(width, height)))
    radius_sq = radius_px * radius_px

    image_rows = bytearray()
    for y in range(height):
        image_rows.append(0)  # PNG row filter: None
        dy = y - point.y
        for x in range(width):
            dx = x - point.x
            image_rows.append(255 if (dx * dx + dy * dy) <= radius_sq else 0)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(image_rows), level=9)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


async def upload_binary_blob(
    *,
    data: bytes,
    content_type: str,
    file_name: str,
) -> str:
    _require_fal_key()

    try:
        return await fal_client.upload_async(
            data=data,
            content_type=content_type,
            file_name=file_name,
        )
    except Exception as exc:
        logger.exception(
            "fal.ai upload failed | file_name={} | content_type={} | size_bytes={}",
            file_name,
            content_type,
            len(data),
        )
        raise ObjectReplaceFalError("fal.ai upload failed") from exc


def _extract_request_id(output: dict[str, Any], enqueued_request_id: str | None) -> str | None:
    request_id = output.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return enqueued_request_id


def extract_mask_url(output: dict[str, Any]) -> str:
    mask_url = output.get("mask_url")
    if isinstance(mask_url, str) and mask_url:
        return mask_url

    mask = output.get("mask")
    if isinstance(mask, dict):
        url = mask.get("url")
        if isinstance(url, str) and url:
            return url

    masks = output.get("masks")
    if isinstance(masks, list):
        for item in masks:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                return url

    image = output.get("image")
    if isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, str) and url:
            return url

    raise ObjectReplaceFalError("fal.ai segmentation response did not include a mask URL")


def extract_fill_image_url(output: dict[str, Any]) -> str:
    images = output.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                return url

    raise ObjectReplaceFalError("fal.ai fill response did not include an image URL")


def _log_queue_update(stage: str) -> Callable[[Any], None]:
    def _handler(update: Any) -> None:
        status = getattr(update, "status", None)
        if status is None and isinstance(update, dict):
            status = update.get("status")
        if status != "IN_PROGRESS":
            return

        logs = getattr(update, "logs", None)
        if logs is None and isinstance(update, dict):
            logs = update.get("logs")
        if not isinstance(logs, list):
            return

        for log in logs:
            message = log.get("message") if isinstance(log, dict) else getattr(log, "message", None)
            if message:
                logger.debug("[object-replace:{}] {}", stage, message)

    return _handler


async def _subscribe(
    *,
    model_id: str,
    arguments: dict[str, Any],
    stage: str,
) -> tuple[dict[str, Any], str | None]:
    _require_fal_key()
    enqueued_request_id: str | None = None

    def _capture_request_id(request_id: str) -> None:
        nonlocal enqueued_request_id
        enqueued_request_id = request_id

    try:
        output = await fal_client.subscribe_async(
            model_id,
            arguments=arguments,
            with_logs=True,
            on_enqueue=_capture_request_id,
            on_queue_update=_log_queue_update(stage),
            client_timeout=settings.fal_timeout_ms / 1000,
        )
    except Exception as exc:
        logger.exception("fal.ai {} request failed | model={}", stage, model_id)
        raise ObjectReplaceFalError(f"fal.ai {stage} request failed") from exc

    if not isinstance(output, dict):
        raise ObjectReplaceFalError(
            f"Invalid fal.ai {stage} response type: {type(output).__name__}"
        )

    return output, _extract_request_id(output, enqueued_request_id)


async def generate_mask(
    *,
    image_url: str,
    point: ObjectReplacePoint,
    item_type: str = "furniture",
) -> tuple[str, str | None]:
    model_id = settings.fal_segmentation_model_id
    if model_id == "fal-ai/fast-sam":
        arguments = {
            "image_url": image_url,
            "points": [
                {
                    "x": point.x,
                    "y": point.y,
                    "label": 1,
                }
            ],
        }
    else:
        arguments = {
            "image_url": image_url,
            "prompt": item_type,
            "point_prompts": [
                {
                    "x": point.x,
                    "y": point.y,
                    "label": 1,
                    "object_id": 1,
                }
            ],
            "apply_mask": False,
            "output_format": "png",
            "return_multiple_masks": True,
            "max_masks": 1,
            "include_scores": True,
            "include_boxes": True,
        }

    output, request_id = await _subscribe(
        model_id=model_id,
        arguments=arguments,
        stage="mask",
    )
    return extract_mask_url(output), request_id


async def inpaint_object(
    *,
    image_url: str,
    mask_url: str,
    prompt: str,
) -> tuple[str, str | None, str]:
    output, request_id = await _subscribe(
        model_id=settings.fal_fill_model_id,
        arguments={
            "image_url": image_url,
            "mask_url": mask_url,
            "prompt": prompt,
            "enhance_prompt": True,
            "num_images": 1,
            "output_format": "jpeg",
            "safety_tolerance": "2",
        },
        stage="fill",
    )
    final_prompt = output.get("prompt")
    return (
        extract_fill_image_url(output),
        request_id,
        final_prompt if isinstance(final_prompt, str) else prompt,
    )


async def replace_object(
    *,
    image_url: str,
    point: ObjectReplacePoint,
    prompt: str,
    item_type: str = "furniture",
) -> dict[str, str | None]:
    mask_url, mask_request_id = await generate_mask(
        image_url=image_url,
        point=point,
        item_type=item_type,
    )
    image_url_out, fill_request_id, final_prompt = await inpaint_object(
        image_url=image_url,
        mask_url=mask_url,
        prompt=prompt,
    )

    return {
        "image_url": image_url_out,
        "mask_url": mask_url,
        "request_id": fill_request_id,
        "mask_request_id": mask_request_id,
        "fill_request_id": fill_request_id,
        "prompt": final_prompt,
    }


async def replace_object_from_uploaded_image(
    *,
    image_bytes: bytes,
    image_content_type: str,
    file_name: str,
    point: ObjectReplacePoint,
    prompt: str,
    item_type: str = "furniture",
    image_width: int,
    image_height: int,
) -> dict[str, str | None]:
    safe_stem = Path(file_name).stem or "object-replace"

    original_image_url = await upload_binary_blob(
        data=image_bytes,
        content_type=image_content_type,
        file_name=file_name,
    )
    try:
        mask_url, mask_request_id = await generate_mask(
            image_url=original_image_url,
            point=point,
            item_type=item_type,
        )
    except ObjectReplaceFalError:
        logger.warning(
            "fal.ai segmentation failed; falling back to circular object-replace mask"
        )
        mask_png = build_circular_mask_png(
            width=image_width,
            height=image_height,
            point=point,
        )
        mask_url = await upload_binary_blob(
            data=mask_png,
            content_type="image/png",
            file_name=f"{safe_stem}-mask.png",
        )
        mask_request_id = None

    image_url_out, fill_request_id, final_prompt = await inpaint_object(
        image_url=original_image_url,
        mask_url=mask_url,
        prompt=prompt,
    )

    return {
        "image_url": image_url_out,
        "mask_url": mask_url,
        "original_image_url": original_image_url,
        "request_id": fill_request_id,
        "mask_request_id": mask_request_id,
        "fill_request_id": fill_request_id,
        "prompt": final_prompt,
    }
