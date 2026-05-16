from __future__ import annotations

import uuid

from pydantic import ValidationError
import pytest

from app.services.object_replace import fal_service, storage
from app.services.object_replace.schemas import (
    CreateObjectReplaceUploadRequest,
    ObjectReplacePoint,
    ReplaceObjectRequest,
)


def test_upload_schema_rejects_unsupported_content_type() -> None:
    with pytest.raises(ValidationError):
        CreateObjectReplaceUploadRequest(
            file_name="room.gif",
            content_type="image/gif",
            image_width=1200,
            image_height=900,
        )


def test_upload_schema_accepts_optional_dimensions() -> None:
    payload = CreateObjectReplaceUploadRequest(
        file_name="room.jpg",
        content_type="image/jpeg",
    )

    assert payload.image_width is None
    assert payload.image_height is None


def test_upload_schema_rejects_oversized_dimensions() -> None:
    with pytest.raises(ValidationError):
        CreateObjectReplaceUploadRequest(
            file_name="room.jpg",
            content_type="image/jpeg",
            image_width=9000,
            image_height=900,
        )


def test_replace_schema_rejects_invalid_payloads() -> None:
    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="not-a-url",
            prompt="replace sofa",
            point={"x": 10, "y": 10, "label": 1},
            image_width=100,
            image_height=100,
        )

    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="https://example.test/room.jpg",
            prompt="  x  ",
            point={"x": 10, "y": 10, "label": 1},
            image_width=100,
            image_height=100,
        )

    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="https://example.test/room.jpg",
            prompt="replace sofa",
            point={"x": 10, "y": 10, "label": 0},
            image_width=100,
            image_height=100,
        )


def test_replace_schema_rejects_point_outside_declared_dimensions() -> None:
    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="https://example.test/room.jpg",
            prompt="replace sofa",
            point={"x": 100, "y": 50, "label": 1},
            image_width=100,
            image_height=100,
        )

    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="https://example.test/room.jpg",
            prompt="replace sofa",
            point={"x": 50, "y": 100, "label": 1},
            image_width=100,
            image_height=100,
        )


def test_object_replace_key_generation_sanitizes_filename() -> None:
    upload_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    key = storage.build_object_replace_key(
        user_id="user/with spaces",
        upload_id=upload_id,
        file_name="../Modern Sofa!.PNG",
        content_type="image/jpeg",
    )

    assert key == (
        "object-replace/user-with-spaces/11111111-2222-3333-4444-555555555555-Modern-Sofa.jpg"
    )


def test_create_presigned_upload_uses_minimal_required_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeS3Client:
        def generate_presigned_url(self, operation: str, Params: dict, ExpiresIn: int) -> str:
            calls.append((operation, Params))
            return f"https://storage.test/{operation}"

    monkeypatch.setattr(storage.settings, "r2_bucket_name", "test-bucket")
    monkeypatch.setattr(storage.settings, "r2_public_url", "https://cdn.test")
    monkeypatch.setattr(storage.settings, "r2_presigned_url_expiry", 3600)
    monkeypatch.setattr(storage, "_get_s3_client", lambda: FakeS3Client())

    upload = storage.create_presigned_upload(
        user_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        file_name="Living Room!.jpg",
        content_type="image/jpeg",
        size_bytes=12345,
        image_width=1200,
        image_height=900,
    )

    assert upload.headers == {"Content-Type": "image/jpeg"}
    assert calls[0][0] == "put_object"
    assert calls[0][1] == {
        "Bucket": "test-bucket",
        "Key": upload.object_key,
    }


def test_extract_mask_url_supports_known_fal_response_shapes() -> None:
    assert fal_service.extract_mask_url({"mask_url": "https://cdn.test/mask.png"}) == (
        "https://cdn.test/mask.png"
    )
    assert fal_service.extract_mask_url({"mask": {"url": "https://cdn.test/mask.png"}}) == (
        "https://cdn.test/mask.png"
    )
    assert fal_service.extract_mask_url({"masks": [{"url": "https://cdn.test/mask.png"}]}) == (
        "https://cdn.test/mask.png"
    )
    assert fal_service.extract_mask_url({"image": {"url": "https://cdn.test/mask.png"}}) == (
        "https://cdn.test/mask.png"
    )


def test_extract_mask_url_rejects_malformed_response() -> None:
    with pytest.raises(fal_service.ObjectReplaceFalError):
        fal_service.extract_mask_url({"masks": [{}]})


def test_extract_fill_image_url_rejects_malformed_response() -> None:
    with pytest.raises(fal_service.ObjectReplaceFalError):
        fal_service.extract_fill_image_url({"images": [{}]})


@pytest.mark.asyncio
async def test_replace_object_calls_fal_segmentation_then_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(fal_service.settings, "fal_key", "test-fal-key")
    monkeypatch.setattr(fal_service.settings, "fal_timeout_ms", 900000)
    monkeypatch.setattr(fal_service.settings, "fal_segmentation_model_id", "fal-ai/fast-sam")
    monkeypatch.setattr(fal_service.settings, "fal_fill_model_id", "fal-ai/flux-pro/v1/fill")

    async def fake_subscribe_async(model_id: str, arguments: dict, **kwargs):
        on_enqueue = kwargs.get("on_enqueue")
        if on_enqueue:
            on_enqueue(f"request-for-{model_id}")

        calls.append((model_id, arguments))
        if model_id == "fal-ai/fast-sam":
            return {"mask_url": "https://cdn.test/mask.png"}
        return {
            "images": [{"url": "https://cdn.test/fill.jpg"}],
            "prompt": "enhanced replacement prompt",
        }

    monkeypatch.setattr(fal_service.fal_client, "subscribe_async", fake_subscribe_async)

    result = await fal_service.replace_object(
        image_url="https://cdn.test/room.jpg",
        point=ObjectReplacePoint(x=42, y=84, label=1),
        prompt="replace sofa with modern beige sectional",
    )

    assert result == {
        "image_url": "https://cdn.test/fill.jpg",
        "mask_url": "https://cdn.test/mask.png",
        "request_id": "request-for-fal-ai/flux-pro/v1/fill",
        "mask_request_id": "request-for-fal-ai/fast-sam",
        "fill_request_id": "request-for-fal-ai/flux-pro/v1/fill",
        "prompt": "enhanced replacement prompt",
    }
    assert calls == [
        (
            "fal-ai/fast-sam",
            {
                "image_url": "https://cdn.test/room.jpg",
                "points": [{"x": 42, "y": 84, "label": 1}],
            },
        ),
        (
            "fal-ai/flux-pro/v1/fill",
            {
                "image_url": "https://cdn.test/room.jpg",
                "mask_url": "https://cdn.test/mask.png",
                "prompt": "replace sofa with modern beige sectional",
                "enhance_prompt": True,
                "num_images": 1,
                "output_format": "jpeg",
                "safety_tolerance": "2",
            },
        ),
    ]
