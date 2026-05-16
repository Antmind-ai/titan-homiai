from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.config import settings
from app.services.object_replace import fal_service, storage
from app.services.object_replace.schemas import (
    CreateObjectReplaceUploadRequest,
    ObjectReplacePoint,
    ObjectReplaceResponse,
    ObjectReplaceUploadResponse,
    ReplaceObjectRequest,
    SUPPORTED_IMAGE_CONTENT_TYPES,
)
from app.services.platform.endpoints.auth import get_current_user_id

router = APIRouter(prefix="/object-replace", tags=["Object Replace"])


@router.post(
    "/uploads",
    response_model=ObjectReplaceUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a presigned upload URL for Object Replace",
)
async def create_object_replace_upload(
    payload: CreateObjectReplaceUploadRequest,
    current_user_id=Depends(get_current_user_id),
) -> ObjectReplaceUploadResponse:
    try:
        upload = await storage.create_presigned_upload_async(
            user_id=current_user_id,
            file_name=payload.file_name,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            image_width=payload.image_width,
            image_height=payload.image_height,
        )
    except storage.ObjectReplaceStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return ObjectReplaceUploadResponse(
        upload_id=upload.upload_id,
        object_key=upload.object_key,
        upload_url=upload.upload_url,
        original_image_url=upload.original_image_url,
        headers=upload.headers,
        expires_in=upload.expires_in,
    )


@router.post(
    "/from-upload",
    response_model=ObjectReplaceResponse,
    summary="Replace an object using a direct image upload",
)
async def replace_object_from_upload(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    point_x: int = Form(...),
    point_y: int = Form(...),
    image_width: int | None = Form(default=None),
    image_height: int | None = Form(default=None),
    _current_user_id=Depends(get_current_user_id),
) -> ObjectReplaceResponse:
    content_type = (file.content_type or "").lower()
    if content_type not in SUPPORTED_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPG, PNG, WEBP, and HEIC images are supported",
        )

    normalized_prompt = prompt.strip()
    if len(normalized_prompt) < 3 or len(normalized_prompt) > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt must contain 3-1000 non-whitespace characters",
        )

    if point_x < 0 or point_x > 8191 or point_y < 0 or point_y > 8191:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="point coordinates must be within 0..8191",
        )

    if image_width is not None and (image_width < 1 or image_width > 8192):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_width must be within 1..8192",
        )

    if image_height is not None and (image_height < 1 or image_height > 8192):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_height must be within 1..8192",
        )

    if image_width is not None and point_x >= image_width:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="point_x must be inside image_width",
        )

    if image_height is not None and point_y >= image_height:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="point_y must be inside image_height",
        )

    try:
        image_bytes = await file.read()
    finally:
        await file.close()

    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded image is empty",
        )

    size_bytes = len(image_bytes)
    if size_bytes > settings.design_upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                "Image exceeds max size of "
                f"{settings.design_upload_max_mb}MB "
                f"(received {(size_bytes / (1024 * 1024)):.2f}MB)"
            ),
        )

    resolved_width = image_width or max(point_x + 1, 1024)
    resolved_height = image_height or max(point_y + 1, 1024)
    point = ObjectReplacePoint(x=point_x, y=point_y, label=1)

    try:
        result = await fal_service.replace_object_from_uploaded_image(
            image_bytes=image_bytes,
            image_content_type=content_type,
            file_name=file.filename or "object-replace-upload",
            point=point,
            prompt=normalized_prompt,
            image_width=resolved_width,
            image_height=resolved_height,
        )
    except fal_service.ObjectReplaceFalError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return ObjectReplaceResponse(
        image_url=str(result["image_url"]),
        mask_url=str(result["mask_url"]),
        original_image_url=str(result["original_image_url"]),
        request_id=result["request_id"],
        mask_request_id=result["mask_request_id"],
        fill_request_id=result["fill_request_id"],
        prompt=str(result["prompt"]),
    )


@router.post(
    "",
    response_model=ObjectReplaceResponse,
    summary="Replace an object in an uploaded image using a tap point and prompt",
)
async def replace_object_in_image(
    payload: ReplaceObjectRequest,
    _current_user_id=Depends(get_current_user_id),
) -> ObjectReplaceResponse:
    try:
        result = await fal_service.replace_object(
            image_url=payload.original_image_url,
            point=payload.point,
            prompt=payload.prompt,
        )
    except fal_service.ObjectReplaceFalError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return ObjectReplaceResponse(
        image_url=str(result["image_url"]),
        mask_url=str(result["mask_url"]),
        original_image_url=payload.original_image_url,
        request_id=result["request_id"],
        mask_request_id=result["mask_request_id"],
        fill_request_id=result["fill_request_id"],
        prompt=str(result["prompt"]),
    )
