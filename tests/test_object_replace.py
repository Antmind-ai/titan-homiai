from __future__ import annotations

import uuid
import zlib

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.services.object_replace import fal_service, storage
from app.services.object_replace import router as object_replace_router
from app.services.object_replace.schemas import (
    CreateObjectReplaceUploadRequest,
    ObjectReplacePoint,
    ReplaceObjectRequest,
)


class _FakeObjectReplaceDB:
    def __init__(self):
        self.added: list[object] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def refresh(self, _value):
        return None


class _FakeUploadFile:
    filename = "room.jpg"
    content_type = "image/jpeg"

    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    async def read(self) -> bytes:
        return self._data

    async def close(self) -> None:
        self.closed = True


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

    with pytest.raises(ValidationError):
        ReplaceObjectRequest(
            original_image_url="https://example.test/room.jpg",
            prompt="replace sofa",
            point={"x": 10, "y": 10, "label": 1},
            item_type="x",
            image_width=100,
            image_height=100,
        )


def test_replace_schema_defaults_and_normalizes_item_type() -> None:
    default_payload = ReplaceObjectRequest(
        original_image_url="https://example.test/room.jpg",
        prompt="replace sofa",
        point={"x": 10, "y": 10, "label": 1},
        image_width=100,
        image_height=100,
    )
    custom_payload = ReplaceObjectRequest(
        original_image_url="https://example.test/room.jpg",
        prompt="replace sofa",
        point={"x": 10, "y": 10, "label": 1},
        item_type="  TV console  ",
        image_width=100,
        image_height=100,
    )

    assert default_payload.item_type == "furniture"
    assert custom_payload.item_type == "TV console"


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
async def test_replace_object_from_uploaded_image_uses_segmentation_mask_fill_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[tuple[str, str, int]] = []

    async def _fake_upload_binary_blob(*, data: bytes, content_type: str, file_name: str) -> str:
        uploads.append((content_type, file_name, len(data)))
        return f"https://files.test/{len(uploads)}"

    async def _fake_generate_mask(
        *,
        image_url: str,
        point: ObjectReplacePoint,
        item_type: str,
        expected_width: int | None,
        expected_height: int | None,
    ):
        assert image_url == "https://files.test/1"
        assert point == ObjectReplacePoint(x=40, y=30, label=1)
        assert item_type == "chair"
        assert expected_width == 128
        assert expected_height == 96
        return "https://files.test/mask.png", "mask-request-1"

    async def _fake_inpaint_object(*, image_url: str, mask_url: str, prompt: str):
        assert image_url == "https://files.test/1"
        assert mask_url == "https://files.test/mask.png"
        assert prompt == "replace chair with modern chair"
        return "https://files.test/output.jpg", "fill-request-1", "enhanced prompt"

    monkeypatch.setattr(fal_service, "upload_binary_blob", _fake_upload_binary_blob)
    monkeypatch.setattr(fal_service, "generate_mask", _fake_generate_mask)
    monkeypatch.setattr(fal_service, "inpaint_object", _fake_inpaint_object)

    result = await fal_service.replace_object_from_uploaded_image(
        image_bytes=b"image-bytes",
        image_content_type="image/jpeg",
        file_name="room.jpg",
        point=ObjectReplacePoint(x=40, y=30, label=1),
        prompt="replace chair with modern chair",
        item_type="chair",
        image_width=128,
        image_height=96,
    )

    assert uploads[0][0] == "image/jpeg"
    assert uploads[0][1] == "room.jpg"
    assert uploads[0][2] == len(b"image-bytes")
    assert len(uploads) == 1

    assert result == {
        "image_url": "https://files.test/output.jpg",
        "mask_url": "https://files.test/mask.png",
        "original_image_url": "https://files.test/1",
        "request_id": "fill-request-1",
        "mask_request_id": "mask-request-1",
        "fill_request_id": "fill-request-1",
        "prompt": "enhanced prompt",
    }


@pytest.mark.asyncio
async def test_replace_object_from_uploaded_image_falls_back_to_circular_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[tuple[str, str, int]] = []

    async def _fake_upload_binary_blob(*, data: bytes, content_type: str, file_name: str) -> str:
        uploads.append((content_type, file_name, len(data)))
        return f"https://files.test/{len(uploads)}"

    async def _raise_generate_mask(**_kwargs):
        raise fal_service.ObjectReplaceFalError("segmentation failed")

    async def _fake_inpaint_object(*, image_url: str, mask_url: str, prompt: str):
        assert image_url == "https://files.test/1"
        assert mask_url == "https://files.test/2"
        assert prompt == "replace lamp with ceramic lamp"
        return "https://files.test/output.jpg", "fill-request-1", "enhanced prompt"

    monkeypatch.setattr(fal_service, "upload_binary_blob", _fake_upload_binary_blob)
    monkeypatch.setattr(fal_service, "generate_mask", _raise_generate_mask)
    monkeypatch.setattr(fal_service, "inpaint_object", _fake_inpaint_object)

    result = await fal_service.replace_object_from_uploaded_image(
        image_bytes=b"image-bytes",
        image_content_type="image/jpeg",
        file_name="room.jpg",
        point=ObjectReplacePoint(x=40, y=30, label=1),
        prompt="replace lamp with ceramic lamp",
        item_type="lamp",
        image_width=128,
        image_height=96,
    )

    assert uploads[0][0] == "image/jpeg"
    assert uploads[1][0] == "image/png"
    assert uploads[1][1].endswith("-mask.png")
    assert uploads[1][2] > uploads[0][2]
    assert result["mask_url"] == "https://files.test/2"
    assert result["mask_request_id"] is None


def test_persist_uploaded_input_image_writes_preview_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(object_replace_router.settings, "design_upload_dir", str(tmp_path))
    user_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    upload_id, filename = object_replace_router._persist_uploaded_input_image(
        user_id=user_id,
        image_bytes=b"fake-image-bytes",
        content_type="image/webp",
    )

    assert filename == f"{upload_id}.webp"

    stored_path = tmp_path / str(user_id) / filename
    assert stored_path.exists()
    assert stored_path.read_bytes() == b"fake-image-bytes"


@pytest.mark.asyncio
async def test_consume_object_replace_credit_charges_twenty_five_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_user_id = uuid.uuid4()
    db = _FakeObjectReplaceDB()
    observed: dict[str, object] = {}

    async def _fake_consume_credit(*args, **kwargs):
        observed["db"] = args[0]
        observed.update(kwargs)

    monkeypatch.setattr(object_replace_router, "consume_credit", _fake_consume_credit)

    await object_replace_router._consume_object_replace_credit(
        db,
        user_id=current_user_id,
        reference_id="request-123",
    )

    assert observed == {
        "db": db,
        "user_id": current_user_id,
        "source": "object_replace",
        "reason": "Furniture swap submitted",
        "reference_id": "request-123",
        "credits": 25,
    }
    assert db.commit_count == 1
    assert db.rollback_count == 0


@pytest.mark.asyncio
async def test_consume_object_replace_credit_rejects_insufficient_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeObjectReplaceDB()

    async def _fake_consume_credit(*_args, **_kwargs):
        raise object_replace_router.InsufficientCreditsError(
            balance=0,
            required_credits=25,
        )

    monkeypatch.setattr(object_replace_router, "consume_credit", _fake_consume_credit)

    with pytest.raises(HTTPException) as exc_info:
        await object_replace_router._consume_object_replace_credit(
            db,
            user_id=uuid.uuid4(),
            reference_id="request-123",
        )

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == {
        "error": "insufficient_credits",
        "message": "No credits remaining. Add credits to continue.",
        "balance": 0,
        "required_credits": 25,
        "lifetime_free_credits": object_replace_router.settings.free_lifetime_credits,
    }
    assert db.commit_count == 0
    assert db.rollback_count == 1


@pytest.mark.asyncio
async def test_replace_object_from_upload_charges_before_enqueueing_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    current_user_id = uuid.uuid4()
    db = _FakeObjectReplaceDB()
    events: list[str] = []

    monkeypatch.setattr(object_replace_router.settings, "design_upload_dir", str(tmp_path))

    async def _fake_consume_object_replace_credit(*args, **kwargs):
        assert args[0] is db
        assert kwargs["user_id"] == current_user_id
        assert kwargs["reference_id"]
        assert kwargs["commit"] is False
        events.append("credit")

    async def _fake_enqueue_job(function_name: str, **kwargs):
        events.append("enqueue")
        assert events == ["credit", "enqueue"]
        assert function_name == "process_object_replace_request_task"
        assert kwargs["design_request_id"]
        assert kwargs["image_content_type"] == "image/jpeg"
        assert kwargs["file_name"] == "room.jpg"
        assert kwargs["point_x"] == 10
        assert kwargs["point_y"] == 12
        assert kwargs["item_type"] == "chair"
        assert kwargs["image_width"] == 100
        assert kwargs["image_height"] == 100
        assert kwargs["_defer_by"] == 1
        return "object-replace-job-1"

    monkeypatch.setattr(
        object_replace_router,
        "_consume_object_replace_credit",
        _fake_consume_object_replace_credit,
    )
    monkeypatch.setattr(object_replace_router, "enqueue_job", _fake_enqueue_job)

    response = await object_replace_router.replace_object_from_upload(
        file=_FakeUploadFile(b"image-bytes"),
        prompt="replace chair with modern chair",
        point_x=10,
        point_y=12,
        image_width=100,
        image_height=100,
        item_type="chair",
        building_type="living-room",
        style_id="modern",
        palette_id="walnut",
        current_user_id=current_user_id,
        db=db,
    )

    assert events == ["credit", "enqueue"]
    assert response.design_request_id == db.added[0].id
    assert response.status == "queued"
    assert response.queue_job_id == "object-replace-job-1"
    assert db.flush_count == 1
    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert len(db.added) == 1
    assert db.added[0].status == "queued"
    assert db.added[0].queue_job_id == "object-replace-job-1"


@pytest.mark.asyncio
async def test_replace_object_calls_fal_segmentation_then_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(fal_service.settings, "fal_key", "test-fal-key")
    monkeypatch.setattr(fal_service.settings, "fal_timeout_ms", 900000)
    monkeypatch.setattr(fal_service.settings, "fal_segmentation_model_id", "fal-ai/sam-3-1/image")
    monkeypatch.setattr(fal_service.settings, "fal_fill_model_id", "fal-ai/flux-pro/v1/fill")

    async def fake_subscribe_async(model_id: str, arguments: dict, **kwargs):
        on_enqueue = kwargs.get("on_enqueue")
        if on_enqueue:
            on_enqueue(f"request-for-{model_id}")

        calls.append((model_id, arguments))
        if model_id == "fal-ai/sam-3-1/image":
            return {"masks": [{"url": "https://cdn.test/mask.png"}]}
        return {
            "images": [{"url": "https://cdn.test/fill.jpg"}],
            "prompt": "enhanced replacement prompt",
        }

    monkeypatch.setattr(fal_service.fal_client, "subscribe_async", fake_subscribe_async)

    result = await fal_service.replace_object(
        image_url="https://cdn.test/room.jpg",
        point=ObjectReplacePoint(x=42, y=84, label=1),
        prompt="replace sofa with modern beige sectional",
        item_type="sofa",
    )

    assert result == {
        "image_url": "https://cdn.test/fill.jpg",
        "mask_url": "https://cdn.test/mask.png",
        "request_id": "request-for-fal-ai/flux-pro/v1/fill",
        "mask_request_id": "request-for-fal-ai/sam-3-1/image",
        "fill_request_id": "request-for-fal-ai/flux-pro/v1/fill",
        "prompt": "enhanced replacement prompt",
    }
    assert calls == [
        (
            "fal-ai/sam-3-1/image",
            {
                "image_url": "https://cdn.test/room.jpg",
                "prompt": "indoor furniture object, sofa",
                "point_prompts": [{"x": 42, "y": 84, "label": 1, "object_id": 1}],
                "apply_mask": False,
                "output_format": "png",
                "return_multiple_masks": True,
                "max_masks": 1,
                "include_scores": True,
                "include_boxes": True,
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
