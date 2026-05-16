from __future__ import annotations

import uuid
import zlib

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


def test_build_circular_mask_png_marks_selected_region() -> None:
    png = fal_service.build_circular_mask_png(
        width=32,
        height=24,
        point=ObjectReplacePoint(x=12, y=10, label=1),
        radius=4,
    )

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png[12:16] == b"IHDR"
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    assert width == 32
    assert height == 24

    offset = 8
    idat_parts: list[bytes] = []
    while offset < len(png):
        chunk_len = int.from_bytes(png[offset : offset + 4], "big")
        chunk_type = png[offset + 4 : offset + 8]
        chunk_data = png[offset + 8 : offset + 8 + chunk_len]
        offset += 12 + chunk_len
        if chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        if chunk_type == b"IEND":
            break

    raw = zlib.decompress(b"".join(idat_parts))
    row_stride = 1 + width
    center_pixel = raw[row_stride * 10 + 1 + 12]
    corner_pixel = raw[row_stride * 0 + 1 + 0]
    assert center_pixel == 255
    assert corner_pixel == 0


@pytest.mark.asyncio
async def test_upload_binary_blob_wraps_fal_upload_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fal_service.settings, "fal_key", "test-fal-key")

    async def _raise_upload_error(**_kwargs):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(fal_service.fal_client, "upload_async", _raise_upload_error)

    with pytest.raises(fal_service.ObjectReplaceFalError):
        await fal_service.upload_binary_blob(
            data=b"hello",
            content_type="image/jpeg",
            file_name="room.jpg",
        )


@pytest.mark.asyncio
async def test_replace_object_from_uploaded_image_uses_mask_fill_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[tuple[str, str, int]] = []

    async def _fake_upload_binary_blob(*, data: bytes, content_type: str, file_name: str) -> str:
        uploads.append((content_type, file_name, len(data)))
        return f"https://files.test/{len(uploads)}"

    async def _fake_inpaint_object(*, image_url: str, mask_url: str, prompt: str):
        assert image_url == "https://files.test/1"
        assert mask_url == "https://files.test/2"
        assert prompt == "replace chair with modern chair"
        return "https://files.test/output.jpg", "fill-request-1", "enhanced prompt"

    monkeypatch.setattr(fal_service, "upload_binary_blob", _fake_upload_binary_blob)
    monkeypatch.setattr(fal_service, "inpaint_object", _fake_inpaint_object)

    result = await fal_service.replace_object_from_uploaded_image(
        image_bytes=b"image-bytes",
        image_content_type="image/jpeg",
        file_name="room.jpg",
        point=ObjectReplacePoint(x=40, y=30, label=1),
        prompt="replace chair with modern chair",
        image_width=128,
        image_height=96,
    )

    assert uploads[0][0] == "image/jpeg"
    assert uploads[0][1] == "room.jpg"
    assert uploads[0][2] == len(b"image-bytes")
    assert uploads[1][0] == "image/png"
    assert uploads[1][1].endswith("-mask.png")
    assert uploads[1][2] > uploads[0][2]

    assert result == {
        "image_url": "https://files.test/output.jpg",
        "mask_url": "https://files.test/2",
        "original_image_url": "https://files.test/1",
        "request_id": "fill-request-1",
        "mask_request_id": None,
        "fill_request_id": "fill-request-1",
        "prompt": "enhanced prompt",
    }


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
