from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_db_context
from app.services.design_generation import DesignGenerationError, generate_image
from app.services.platform.models import DesignRequest, DeviceUser
from app.services.r2 import (
    delete_object_async,
    download_to_path_async,
)


async def health_ping_task(ctx: dict[str, Any], source: str = "api") -> dict[str, str]:
    logger.info("ARQ job received | source={}", source)
    return {
        "status": "ok",
        "source": source,
        "processed_at": datetime.now(UTC).isoformat(),
    }


def _build_design_prompt(design_request: DesignRequest) -> str:
    parts = [design_request.prompt or ""]
    if design_request.building_type:
        parts.append(f"Building type: {design_request.building_type}")
    if design_request.style_id:
        parts.append(f"Style: {design_request.style_id}")
    if design_request.palette_id:
        parts.append(f"Color palette: {design_request.palette_id}")
    return ". ".join(p for p in parts if p)


async def _resolve_input_image_path(design_request: DesignRequest) -> tuple[Path, bool]:
    is_temp = False

    if design_request.input_r2_key:
        temp_dir = Path(settings.design_upload_dir) / str(design_request.user_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        filename = design_request.input_r2_key.rsplit("/", 1)[-1]
        local_path = temp_dir / filename

        logger.debug(
            "Downloading design input from R2 | key={} | local_path={}",
            design_request.input_r2_key,
            local_path,
        )
        await download_to_path_async(design_request.input_r2_key, local_path)
        is_temp = True
        return local_path, is_temp

    if design_request.input_filename:
        image_path = (
            Path(settings.design_upload_dir)
            / str(design_request.user_id)
            / design_request.input_filename
        )

        if not image_path.exists():
            raise DesignGenerationError(f"Input image not found on disk: {image_path}")

        return image_path, is_temp

    raise DesignGenerationError("No input image filename or R2 key on design request")


def _cleanup_temp_file(file_path: Path) -> None:
    try:
        if file_path.exists():
            file_path.unlink()
            logger.debug("Cleaned up temp design input | path={}", file_path)
    except OSError as exc:
        logger.warning("Failed to cleanup temp design input | path={} | error={}", file_path, exc)


async def process_design_request_task(
    ctx: dict[str, Any],
    design_request_id: str,
) -> dict[str, Any]:
    try:
        request_id = uuid.UUID(design_request_id)
    except ValueError as exc:
        logger.error("Invalid design_request_id for ARQ task | id={}", design_request_id)
        raise RuntimeError("Invalid design_request_id") from exc

    logger.info("Processing design request ARQ job | request_id={}", request_id)

    async with get_db_context() as db:
        result = await db.execute(select(DesignRequest).where(DesignRequest.id == request_id))
        design_request = result.scalar_one_or_none()

        if design_request is None:
            logger.error("Design request not found for ARQ task | request_id={}", request_id)
            return {"status": "missing", "design_request_id": design_request_id}

        design_request.status = "processing"
        design_request.processing_started_at = datetime.now(UTC)
        design_request.failed_at = None
        design_request.error_message = None
        await db.commit()

    temp_path: Path | None = None

    try:
        image_path, is_temp = await _resolve_input_image_path(design_request)
        if is_temp:
            temp_path = image_path

        prompt = _build_design_prompt(design_request)

        logger.info(
            "Calling design generation provider | request_id={} | provider={} | "
            "prompt={} | image={}",
            request_id,
            settings.design_generation_provider,
            prompt[:200],
            image_path,
        )

        result_obj = await generate_image(
            prompt=prompt,
            image_path=str(image_path),
        )

        logger.info(
            "Design generation result | request_id={} | url={}",
            request_id,
            result_obj.url,
        )

        async with get_db_context() as db:
            result = await db.execute(select(DesignRequest).where(DesignRequest.id == request_id))
            design_request = result.scalar_one_or_none()

            if design_request is None:
                logger.error("Design request missing during completion | request_id={}", request_id)
                return {"status": "missing", "design_request_id": design_request_id}

            design_request.status = "completed"
            design_request.completed_at = datetime.now(UTC)
            design_request.output_preview_url = result_obj.url
            await db.commit()

    except DesignGenerationError as exc:
        error_msg = str(exc)[:500]
        logger.error("Design generation failed | request_id={} | error={}", request_id, error_msg)

        async with get_db_context() as db:
            result = await db.execute(select(DesignRequest).where(DesignRequest.id == request_id))
            design_request = result.scalar_one_or_none()

            if design_request is not None:
                design_request.status = "failed"
                design_request.failed_at = datetime.now(UTC)
                design_request.error_message = error_msg
                await db.commit()

        return {
            "status": "failed",
            "design_request_id": design_request_id,
            "error": error_msg,
        }

    except Exception as exc:
        error_msg = str(exc)[:500]
        logger.exception("Design request ARQ task failed | request_id={}", request_id)

        async with get_db_context() as db:
            result = await db.execute(select(DesignRequest).where(DesignRequest.id == request_id))
            design_request = result.scalar_one_or_none()

            if design_request is not None:
                design_request.status = "failed"
                design_request.failed_at = datetime.now(UTC)
                design_request.error_message = error_msg
                await db.commit()

        raise

    finally:
        if temp_path is not None:
            _cleanup_temp_file(temp_path)

    logger.info("Design request completed by ARQ worker | request_id={}", request_id)
    return {
        "status": "completed",
        "design_request_id": design_request_id,
        "output_url": result_obj.url,
    }


async def cleanup_user_data_task(
    ctx: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        logger.error("Invalid user_id for cleanup task | id={}", user_id)
        raise RuntimeError("Invalid user_id") from exc

    logger.info("Starting user data cleanup | user_id={}", uid)

    deleted_keys: list[str] = []
    deleted_files: list[str] = []

    async with get_db_context() as db:
        result = await db.execute(
            select(DeviceUser).where(DeviceUser.id == uid)
        )
        user = result.scalar_one_or_none()

        if user is None:
            logger.warning("User not found for cleanup | user_id={}", uid)
            return {"status": "missing", "user_id": user_id}

        result = await db.execute(
            select(DesignRequest.input_r2_key).where(
                DesignRequest.user_id == uid,
                DesignRequest.input_r2_key.isnot(None),
            )
        )
        r2_keys = [row[0] for row in result.all() if row[0]]

        result = await db.execute(
            select(DesignRequest.input_filename).where(
                DesignRequest.user_id == uid,
                DesignRequest.input_filename.isnot(None),
            )
        )
        filenames = [row[0] for row in result.all() if row[0]]

    for key in r2_keys:
        try:
            deleted = await delete_object_async(key)
            if deleted:
                deleted_keys.append(key)
        except Exception as exc:
            logger.error(
                "Failed to delete R2 object | user_id={} | key={} | error={}",
                uid,
                key,
                exc,
            )

    for filename in filenames:
        file_path = Path(settings.design_upload_dir) / str(uid) / filename
        try:
            if file_path.exists():
                file_path.unlink()
                deleted_files.append(str(file_path))
        except OSError as exc:
            logger.error(
                "Failed to delete design file | user_id={} | path={} | error={}",
                uid,
                file_path,
                exc,
            )

    user_dir = Path(settings.design_upload_dir) / str(uid)
    try:
        if user_dir.exists() and user_dir.is_dir():
            remaining = list(user_dir.iterdir())
            if not remaining:
                user_dir.rmdir()
    except OSError as exc:
        logger.error(
            "Failed to remove user directory | user_id={} | path={} | error={}",
            uid,
            user_dir,
            exc,
        )

    logger.info(
        "User data cleanup completed | user_id={} | deleted_r2_objects={} | deleted_local_files={}",
        uid,
        len(deleted_keys),
        len(deleted_files),
    )

    return {
        "status": "completed",
        "user_id": user_id,
        "deleted_r2_keys": deleted_keys,
        "deleted_local_files": deleted_files,
        "deleted_count": len(deleted_keys) + len(deleted_files),
    }
