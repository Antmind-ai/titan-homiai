from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_IMAGE_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic")
SupportedImageContentType = Literal["image/jpeg", "image/png", "image/webp", "image/heic"]


def normalize_item_type(value: str | None) -> str:
    item_type = (value or "furniture").strip()
    if len(item_type) < 3:
        raise ValueError("item_type must contain at least 3 non-whitespace characters")
    return item_type[:80]


def _validate_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must include http or https scheme and host")
    return value


class CreateObjectReplaceUploadRequest(BaseModel):
    file_name: str = Field(..., min_length=1, max_length=180)
    content_type: SupportedImageContentType
    size_bytes: int | None = Field(default=None, ge=1, le=30 * 1024 * 1024)
    image_width: int | None = Field(default=None, ge=1, le=8192)
    image_height: int | None = Field(default=None, ge=1, le=8192)

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str) -> str:
        file_name = value.strip()
        if not file_name:
            raise ValueError("file_name cannot be empty")
        return file_name


class ObjectReplaceUploadResponse(BaseModel):
    upload_id: uuid.UUID
    object_key: str
    upload_url: str
    original_image_url: str
    headers: dict[str, str]
    expires_in: int

    @field_validator("upload_url", "original_image_url")
    @classmethod
    def validate_urls(cls, value: str) -> str:
        return _validate_http_url(value)


class ObjectReplacePoint(BaseModel):
    x: int = Field(..., ge=0, le=8191)
    y: int = Field(..., ge=0, le=8191)
    label: Literal[1] = 1


class ReplaceObjectRequest(BaseModel):
    original_image_url: str = Field(..., max_length=3000)
    prompt: str = Field(..., min_length=3, max_length=1000)
    point: ObjectReplacePoint
    item_type: str = Field(default="furniture", min_length=3, max_length=80)
    image_width: int | None = Field(default=None, ge=1, le=8192)
    image_height: int | None = Field(default=None, ge=1, le=8192)

    @field_validator("original_image_url")
    @classmethod
    def validate_original_image_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if len(prompt) < 3:
            raise ValueError("prompt must contain at least 3 non-whitespace characters")
        return prompt

    @field_validator("item_type")
    @classmethod
    def normalize_item_type_value(cls, value: str) -> str:
        return normalize_item_type(value)

    @model_validator(mode="after")
    def validate_point_bounds(self) -> ReplaceObjectRequest:
        if self.image_width is not None and self.point.x >= self.image_width:
            raise ValueError("point.x must be inside image_width")
        if self.image_height is not None and self.point.y >= self.image_height:
            raise ValueError("point.y must be inside image_height")
        return self


class ObjectReplaceResponse(BaseModel):
    image_url: str
    mask_url: str
    original_image_url: str
    request_id: str | None = None
    mask_request_id: str | None = None
    fill_request_id: str | None = None
    prompt: str

    @field_validator("image_url", "mask_url", "original_image_url")
    @classmethod
    def validate_response_urls(cls, value: str) -> str:
        return _validate_http_url(value)


class ObjectReplaceJobResponse(BaseModel):
    design_request_id: uuid.UUID
    status: str
    queue_job_id: str | None = None
    prompt: str
