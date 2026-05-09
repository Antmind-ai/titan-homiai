from datetime import UTC, datetime
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.credit_service import InsufficientCreditsError, consume_credit
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.schemas.design import (
    CreateDesignRequest,
    CreateDesignResponse,
    DesignInputUploadResponse,
    DesignSource,
)

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

    try:
        await consume_credit(
            db,
            user_id=current_user_id,
            source="design_request",
            reason="Design request submitted",
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

    await db.commit()

    return CreateDesignResponse(
        design_request_id=uuid.uuid4(),
        user_id=current_user_id,
        status="queued",
        submitted_at=datetime.now(UTC),
    )