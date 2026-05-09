from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_db_context
from app.services.higgsfield.client import HiggsfieldError, generate_image
from app.services.platform.models import DesignRequest


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


def _resolve_input_image_path(design_request: DesignRequest) -> Path:
    if not design_request.input_filename:
        raise HiggsfieldError("No input image filename on design request")

    image_path = (
        Path(settings.design_upload_dir)
        / str(design_request.user_id)
        / design_request.input_filename
    )

    if not image_path.exists():
        raise HiggsfieldError(f"Input image not found on disk: {image_path}")

    return image_path


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

    try:
        image_path = _resolve_input_image_path(design_request)
        prompt = _build_design_prompt(design_request)

        logger.info(
            "Calling Higgsfield | request_id={} | prompt={} | image={}",
            request_id,
            prompt[:200],
            image_path,
        )

        result_obj = await generate_image(
            model=settings.higgsfield_design_model,
            prompt=prompt,
            image_path=str(image_path),
            quality=settings.higgsfield_design_quality,
            aspect_ratio=settings.higgsfield_design_aspect_ratio,
        )

        logger.info(
            "Higgsfield result | request_id={} | url={}",
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

    except HiggsfieldError as exc:
        error_msg = str(exc)[:500]
        logger.error("Higgsfield generation failed | request_id={} | error={}", request_id, error_msg)

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

    logger.info("Design request completed by ARQ worker | request_id={}", request_id)
    return {
        "status": "completed",
        "design_request_id": design_request_id,
        "output_url": result_obj.url,
    }
