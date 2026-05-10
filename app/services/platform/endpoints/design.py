from datetime import UTC, datetime
import mimetypes
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.credit_service import InsufficientCreditsError, consume_credit
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.models import DesignRequest
from app.services.platform.schemas.design import (
    CreateDesignRequest,
    CreateDesignResponse,
    DesignHistoryItem,
    DesignHistoryResponse,
    DesignInputUploadResponse,
    DesignSource,
)
from app.workers.client import enqueue_job

router = APIRouter(prefix="/designs")

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


def _resolve_upload_suffix(upload_file: UploadFile) -> str:
    filename_suffix = Path(upload_file.filename or "").suffix.lower()
    if filename_suffix:
        return filename_suffix
    return ALLOWED_CONTENT_TYPES.get(upload_file.content_type or "", ".jpg")


def _resolve_user_upload_file(user_id: uuid.UUID, upload_id: uuid.UUID) -> Path | None:
    user_dir = Path(settings.design_upload_dir) / str(user_id)
    if not user_dir.exists():
        return None

    matches = list(user_dir.glob(f"{upload_id}.*"))
    if not matches:
        return None
    return matches[0]


def _resolve_preview_file_for_request(
    user_id: uuid.UUID,
    design_request: DesignRequest,
) -> Path | None:
    if design_request.input_filename:
        file_path = Path(settings.design_upload_dir) / str(user_id) / design_request.input_filename
        if file_path.exists():
            return file_path

    if design_request.input_upload_id is None:
        return None

    return _resolve_user_upload_file(user_id, design_request.input_upload_id)


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
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPG, PNG, WEBP, and HEIC images are supported",
        )

    user_upload_dir = Path(settings.design_upload_dir) / str(current_user_id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    upload_id = uuid.uuid4()
    suffix = _resolve_upload_suffix(file)
    output_path = user_upload_dir / f"{upload_id}{suffix}"
    size_bytes = 0

    try:
        with output_path.open("wb") as output_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size_bytes += len(chunk)
                if size_bytes > settings.design_upload_max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"Image exceeds max size of {settings.design_upload_max_mb}MB"
                        ),
                    )

                output_file.write(chunk)
    except HTTPException:
        if output_path.exists():
            output_path.unlink()
        raise
    finally:
        await file.close()

    return DesignInputUploadResponse(
        upload_id=upload_id,
        user_id=current_user_id,
        filename=output_path.name,
        content_type=file.content_type,
        size_bytes=size_bytes,
    )


@router.get(
    "/me",
    response_model=DesignHistoryResponse,
    summary="List current user's uploaded design requests",
)
async def list_my_design_requests(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DesignHistoryResponse:
    result = await db.execute(
        select(DesignRequest)
        .where(
            DesignRequest.user_id == current_user_id,
            DesignRequest.source == DesignSource.UPLOAD.value,
            DesignRequest.deleted_at.is_(None),
        )
        .order_by(DesignRequest.submitted_at.desc())
        .limit(50)
    )
    design_requests = result.scalars().all()

    history_items: list[DesignHistoryItem] = []
    for design_request in design_requests:
        preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
        history_items.append(
            DesignHistoryItem(
                design_request_id=design_request.id,
                user_id=design_request.user_id,
                source=DesignSource(design_request.source),
                status=design_request.status,
                input_upload_id=design_request.input_upload_id,
                building_type=design_request.building_type,
                style_id=design_request.style_id,
                palette_id=design_request.palette_id,
                prompt=design_request.prompt,
                submitted_at=design_request.submitted_at,
                updated_at=design_request.updated_at,
                preview_url=(
                    _build_preview_url(design_request.id) if preview_file is not None else None
                ),
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

    preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
    return DesignHistoryItem(
        design_request_id=design_request.id,
        user_id=design_request.user_id,
        source=DesignSource(design_request.source),
        status=design_request.status,
        input_upload_id=design_request.input_upload_id,
        building_type=design_request.building_type,
        style_id=design_request.style_id,
        palette_id=design_request.palette_id,
        prompt=design_request.prompt,
        submitted_at=design_request.submitted_at,
        updated_at=design_request.updated_at,
        preview_url=(
            _build_preview_url(design_request.id) if preview_file is not None else None
        ),
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
    summary="Serve uploaded source image preview for a design request",
)
async def get_design_request_preview(
    design_request_id: uuid.UUID,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    result = await db.execute(
        select(DesignRequest).where(
            DesignRequest.id == design_request_id,
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
    )
    design_request = result.scalar_one_or_none()
    if design_request is None or design_request.source != DesignSource.UPLOAD.value:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Design request not found",
        )

    preview_file = _resolve_preview_file_for_request(current_user_id, design_request)
    if preview_file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded design input not found",
        )

    media_type, _ = mimetypes.guess_type(preview_file.name)
    return FileResponse(
        preview_file,
        media_type=media_type or "application/octet-stream",
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
    existing_upload: Path | None = None

    if payload.source == DesignSource.UPLOAD:
        if payload.input_upload_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="input_upload_id is required for uploaded source",
            )

        existing_upload = _resolve_user_upload_file(current_user_id, payload.input_upload_id)
        if existing_upload is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded design input not found",
            )

    design_request = DesignRequest(
        user_id=current_user_id,
        source=payload.source.value,
        input_upload_id=payload.input_upload_id,
        input_filename=existing_upload.name if existing_upload is not None else None,
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
