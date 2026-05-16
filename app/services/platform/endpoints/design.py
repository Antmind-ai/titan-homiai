from datetime import UTC, datetime
import mimetypes
from pathlib import Path
import shutil
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.credit_service import InsufficientCreditsError, consume_credit
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.models import DesignRequest, DiscoverAsset, DiscoverCard
from app.services.platform.schemas.design import (
    CreateDesignRequest,
    CreateDesignResponse,
    DesignHistoryItem,
    DesignHistoryResponse,
    DesignInputUploadResponse,
    DesignSource,
)
from app.services.r2 import (
    build_r2_key,
    generate_presigned_url,
    object_exists,
    upload_file_async,
)
from app.workers.client import enqueue_job

router = APIRouter(prefix="/designs")

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
DISCOVER_SAMPLE_ASSETS_DIR = Path("/app/discover-sample-assets")


def _validate_r2_upload_ownership(
    *,
    current_user_id: uuid.UUID,
    input_upload_id: uuid.UUID,
    input_r2_key: str | None,
) -> str:
    if not input_r2_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="input_r2_key is required for uploaded source",
        )

    expected_prefix = f"{current_user_id}/{input_upload_id}"
    if not input_r2_key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Uploaded design input does not belong to the current user",
        )

    if not object_exists(input_r2_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded design input not found in storage",
        )

    return input_r2_key


def _copy_example_sample_to_user_upload_dir(
    *,
    current_user_id: uuid.UUID,
    example_r2_key: str,
) -> str | None:
    source_filename = Path(example_r2_key).name
    source_path = DISCOVER_SAMPLE_ASSETS_DIR / source_filename
    if not source_path.exists():
        return None

    user_upload_dir = Path(settings.design_upload_dir) / str(current_user_id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    target_filename = f"example-{uuid.uuid4()}-{source_filename}"
    target_path = user_upload_dir / target_filename
    shutil.copyfile(source_path, target_path)
    return target_filename


async def _resolve_example_input_refs(
    *,
    db: AsyncSession,
    current_user_id: uuid.UUID,
    example_photo_id: str | None,
) -> tuple[str | None, str | None]:
    if not example_photo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="example_photo_id is required for example source",
        )

    result = await db.execute(
        select(DiscoverAsset.r2_key)
        .select_from(DiscoverCard)
        .join(
            DiscoverAsset,
            DiscoverAsset.asset_id == DiscoverCard.image_asset_id,
        )
        .where(DiscoverCard.card_id == example_photo_id)
        .limit(1)
    )
    resolved_r2_key = result.scalar_one_or_none()

    if resolved_r2_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Example photo not found",
        )

    if settings.r2_endpoint_url and settings.r2_bucket_name and object_exists(resolved_r2_key):
        return resolved_r2_key, None

    try:
        local_filename = _copy_example_sample_to_user_upload_dir(
            current_user_id=current_user_id,
            example_r2_key=resolved_r2_key,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not prepare local example photo sample",
        ) from exc

    if local_filename:
        return None, local_filename

    if not settings.r2_endpoint_url or not settings.r2_bucket_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured and local sample assets are unavailable",
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Example photo asset not found in storage",
    )


def _resolve_upload_suffix(upload_file: UploadFile) -> str:
    filename_suffix = Path(upload_file.filename or "").suffix.lower()
    if filename_suffix:
        return filename_suffix
    return ALLOWED_CONTENT_TYPES.get(upload_file.content_type or "", ".jpg")


def _resolve_preview_file_for_request(
    user_id: uuid.UUID,
    design_request: DesignRequest,
) -> Path | None:
    if design_request.input_filename:
        file_path = Path(settings.design_upload_dir) / str(user_id) / design_request.input_filename
        if file_path.exists():
            return file_path
    return None


def _build_preview_url(design_request_id: uuid.UUID) -> str:
    return f"{settings.api_v1_prefix}/designs/{design_request_id}/preview"


@router.post(
    "/inputs",
    response_model=DesignInputUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload input image for create design flow",
)
async def upload_design_input_image(
    file: UploadFile = File(...),
    current_user_id: uuid.UUID = Depends(get_current_user_id),
) -> DesignInputUploadResponse:
    if not settings.r2_bucket_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured",
        )

    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPG, PNG, WEBP, and HEIC images are supported",
        )

    upload_id = uuid.uuid4()
    suffix = _resolve_upload_suffix(file)
    filename = f"{upload_id}{suffix}"
    r2_key = build_r2_key(str(current_user_id), filename)

    size_bytes = 0
    chunks: list[bytes] = []

    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break

            size_bytes += len(chunk)
            if size_bytes > settings.design_upload_max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        "Image exceeds max size of "
                        f"{settings.design_upload_max_mb}MB "
                        f"(received {(size_bytes / (1024 * 1024)):.2f}MB)"
                    ),
                )

            chunks.append(chunk)
    finally:
        await file.close()

    await upload_file_async(r2_key, b"".join(chunks), content_type)

    return DesignInputUploadResponse(
        upload_id=upload_id,
        user_id=current_user_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        r2_key=r2_key,
    )


@router.get(
    "/me",
    response_model=DesignHistoryResponse,
    summary="List current user's design requests",
)
async def list_my_design_requests(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DesignHistoryResponse:
    result = await db.execute(
        select(DesignRequest)
        .where(
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
        .order_by(DesignRequest.submitted_at.desc())
        .limit(50)
    )
    design_requests = result.scalars().all()

    history_items: list[DesignHistoryItem] = []
    for design_request in design_requests:
        preview_url: str | None = None
        if design_request.input_r2_key and settings.r2_endpoint_url:
            preview_url = _build_preview_url(design_request.id)
        elif design_request.input_filename:
            preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
            if preview_file:
                preview_url = _build_preview_url(design_request.id)

        history_items.append(
            DesignHistoryItem(
                design_request_id=design_request.id,
                user_id=design_request.user_id,
                source=DesignSource(design_request.source),
                status=design_request.status,
                input_upload_id=design_request.input_upload_id,
                input_r2_key=design_request.input_r2_key,
                building_type=design_request.building_type,
                style_id=design_request.style_id,
                palette_id=design_request.palette_id,
                prompt=design_request.prompt,
                submitted_at=design_request.submitted_at,
                updated_at=design_request.updated_at,
                preview_url=preview_url,
                output_preview_url=design_request.output_preview_url,
            )
        )

    return DesignHistoryResponse(items=history_items)


@router.get(
    "/{design_request_id}",
    response_model=DesignHistoryItem,
    summary="Get a single design request",
)
async def get_design_request(
    design_request_id: uuid.UUID,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DesignHistoryItem:
    result = await db.execute(
        select(DesignRequest).where(
            DesignRequest.id == design_request_id,
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
    )
    design_request = result.scalar_one_or_none()
    if design_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Design request not found",
        )

    preview_url: str | None = None
    if design_request.input_r2_key and settings.r2_endpoint_url:
        preview_url = _build_preview_url(design_request.id)
    elif design_request.input_filename:
        preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
        if preview_file:
            preview_url = _build_preview_url(design_request.id)

    return DesignHistoryItem(
        design_request_id=design_request.id,
        user_id=design_request.user_id,
        source=DesignSource(design_request.source),
        status=design_request.status,
        input_upload_id=design_request.input_upload_id,
        input_r2_key=design_request.input_r2_key,
        building_type=design_request.building_type,
        style_id=design_request.style_id,
        palette_id=design_request.palette_id,
        prompt=design_request.prompt,
        submitted_at=design_request.submitted_at,
        updated_at=design_request.updated_at,
        preview_url=preview_url,
        output_preview_url=design_request.output_preview_url,
    )


@router.delete(
    "/{design_request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a design request",
)
async def delete_design_request(
    design_request_id: uuid.UUID,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(DesignRequest).where(
            DesignRequest.id == design_request_id,
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
    )
    design_request = result.scalar_one_or_none()
    if design_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Design request not found",
        )

    design_request.deleted_at = datetime.now(UTC)
    await db.commit()


@router.get(
    "/{design_request_id}/preview",
    response_model=None,
    summary="Serve source image preview for a design request",
)
async def get_design_request_preview(
    design_request_id: uuid.UUID,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse | FileResponse:
    result = await db.execute(
        select(DesignRequest).where(
            DesignRequest.id == design_request_id,
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
    )
    design_request = result.scalar_one_or_none()
    if design_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Design request not found",
        )

    if design_request.input_r2_key and settings.r2_endpoint_url:
        presigned_url = generate_presigned_url(design_request.input_r2_key)
        return RedirectResponse(url=presigned_url, status_code=302)

    if design_request.input_filename:
        preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
        if preview_file:
            media_type, _ = mimetypes.guess_type(preview_file.name)
            return FileResponse(
                preview_file,
                media_type=media_type or "application/octet-stream",
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Uploaded design input not found",
    )


@router.post(
    "",
    response_model=CreateDesignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit create design flow selections",
)
async def submit_design_request(
    payload: CreateDesignRequest,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> CreateDesignResponse:
    input_filename: str | None = None
    input_r2_key: str | None = None

    if payload.source == DesignSource.UPLOAD:
        if payload.input_upload_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="input_upload_id is required for uploaded source",
            )

        if settings.r2_endpoint_url:
            input_r2_key = _validate_r2_upload_ownership(
                current_user_id=current_user_id,
                input_upload_id=payload.input_upload_id,
                input_r2_key=payload.input_r2_key,
            )
        else:
            user_upload_dir = Path(settings.design_upload_dir) / str(current_user_id)
            matches = list(user_upload_dir.glob(f"{payload.input_upload_id}.*"))
            if not matches:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Uploaded design input not found",
                )
            input_filename = matches[0].name
    else:
        input_r2_key, input_filename = await _resolve_example_input_refs(
            db=db,
            current_user_id=current_user_id,
            example_photo_id=payload.example_photo_id,
        )

    design_request = DesignRequest(
        user_id=current_user_id,
        source=payload.source.value,
        input_upload_id=payload.input_upload_id,
        input_r2_key=input_r2_key,
        input_filename=input_filename,
        example_photo_id=payload.example_photo_id,
        building_type=payload.building_type,
        style_id=payload.style_id,
        palette_id=payload.palette_id,
        prompt=payload.prompt,
        status="queued",
    )
    db.add(design_request)

    try:
        await db.flush()

        await consume_credit(
            db,
            user_id=current_user_id,
            source="design_request",
            reason="Design request submitted",
            reference_id=str(design_request.id),
        )
    except InsufficientCreditsError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_credits",
                "message": "No credits remaining. Add credits to continue.",
                "balance": exc.balance,
                "required_credits": exc.required_credits,
                "lifetime_free_credits": settings.free_lifetime_credits,
            },
        ) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc

    try:
        queue_job_id = await enqueue_job(
            "process_design_request_task",
            design_request_id=str(design_request.id),
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not enqueue design request. Please try again.",
        ) from exc

    design_request.queue_job_id = queue_job_id
    await db.commit()
    await db.refresh(design_request)

    return CreateDesignResponse(
        design_request_id=design_request.id,
        user_id=current_user_id,
        status=design_request.status,
        submitted_at=design_request.submitted_at,
        queue_job_id=design_request.queue_job_id,
        prompt=design_request.prompt,
    )
