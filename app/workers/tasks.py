from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Any
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_context
from app.services.design_generation import DesignGenerationError, generate_image
from app.services.design_generation.service import get_model_credit_cost
from app.services.platform.credit_service import (
    InsufficientCreditsError,
    add_credits,
    consume_credit,
)
from app.services.platform.models import CreditLedgerEvent, DesignRequest
from app.services.r2 import (
    delete_object_async,
    download_to_path_async,
    list_objects_with_prefix_async,
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

    raise DesignGenerationError(
        "No input image filename or R2 key on design request"
        f" | source={design_request.source}"
        f" | example_photo_id={design_request.example_photo_id}"
    )


def _cleanup_temp_file(file_path: Path) -> None:
    try:
        if file_path.exists():
            file_path.unlink()
            logger.debug("Cleaned up temp design input | path={}", file_path)
    except OSError as exc:
        logger.warning("Failed to cleanup temp design input | path={} | error={}", file_path, exc)


def _build_credit_refund_idempotency_key(design_request_id: uuid.UUID) -> str:
    return f"design-request-refund:{design_request_id}"


async def _refund_design_request_credit(
    db: AsyncSession,
    *,
    design_request: DesignRequest,
    failure_reason: str,
) -> bool:
    request_id = str(design_request.id)

    result = await db.execute(
        select(CreditLedgerEvent)
        .where(CreditLedgerEvent.user_id == design_request.user_id)
        .where(CreditLedgerEvent.source == "design_request")
        .where(CreditLedgerEvent.reference_id == request_id)
        .order_by(CreditLedgerEvent.created_at.desc())
        .limit(1)
    )
    original_charge_event = result.scalar_one_or_none()

    if original_charge_event is None:
        logger.warning(
            "Skipping credit restore: original charge event not found | request_id={}",
            request_id,
        )
        return False

    credits_to_restore = abs(int(original_charge_event.delta))
    if credits_to_restore <= 0:
        logger.warning(
            "Skipping credit restore: original charge delta is invalid | "
            "request_id={} | delta={}",
            request_id,
            original_charge_event.delta,
        )
        return False

    refund_reason = f"Design generation failed: {failure_reason}"[:255]
    mutation = await add_credits(
        db,
        user_id=design_request.user_id,
        credits=credits_to_restore,
        source="design_request_refund",
        reason=refund_reason,
        reference_id=request_id,
        idempotency_key=_build_credit_refund_idempotency_key(design_request.id),
    )
    logger.info(
        "Restored design request credits | request_id={} | credits={} | "
        "idempotent={} | balance={}",
        request_id,
        credits_to_restore,
        mutation.idempotent,
        mutation.balance,
    )
    return True


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

            model_cost = get_model_credit_cost(result_obj.model) if result_obj.model else 25
            if model_cost > 25:
                try:
                    await consume_credit(
                        db,
                        user_id=design_request.user_id,
                        source="design_request_fallback",
                        reason=f"Fallback model used: {result_obj.model}",
                        reference_id=design_request_id,
                        credits=model_cost - 25,
                    )
                    logger.info(
                        "Additional credits charged for fallback model | "
                        "request_id={} | model={} | extra_credits={}",
                        request_id,
                        result_obj.model,
                        model_cost - 25,
                    )
                except InsufficientCreditsError as exc:
                    raise DesignGenerationError(
                        "Fallback model required additional credits, "
                        f"but only {exc.balance} credits were available."
                    ) from exc
                except Exception:
                    raise DesignGenerationError(
                        "Could not charge additional credits required for "
                        f"fallback model {result_obj.model}."
                    ) from None

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
                try:
                    await _refund_design_request_credit(
                        db,
                        design_request=design_request,
                        failure_reason=error_msg,
                    )
                except Exception:
                    logger.exception(
                        "Failed to restore design request credits after generation failure | "
                        "request_id={}",
                        request_id,
                    )

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
                try:
                    await _refund_design_request_credit(
                        db,
                        design_request=design_request,
                        failure_reason=error_msg,
                    )
                except Exception:
                    logger.exception(
                        "Failed to restore design request credits after unexpected worker failure | "
                        "request_id={}",
                        request_id,
                    )

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

    # Cleanup only user-owned prefixes to avoid deleting shared discover assets.
    user_prefixes = [
        f"{uid}/",
        f"object-replace/{uid}/",
    ]
    seen_keys: set[str] = set()

    for prefix in user_prefixes:
        try:
            keys = await list_objects_with_prefix_async(prefix)
        except Exception as exc:
            logger.error(
                "Failed to list R2 objects | user_id={} | prefix={} | error={}",
                uid,
                prefix,
                exc,
            )
            continue

        for key in keys:
            if key in seen_keys:
                continue
            seen_keys.add(key)
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

    user_dir = Path(settings.design_upload_dir) / str(uid)
    try:
        if user_dir.exists() and user_dir.is_dir():
            local_files = [str(path) for path in user_dir.rglob("*") if path.is_file()]
            shutil.rmtree(user_dir)
            deleted_files.extend(local_files)
    except OSError as exc:
        logger.error(
            "Failed to remove user upload directory | user_id={} | path={} | error={}",
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
