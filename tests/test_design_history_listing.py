from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi.responses import RedirectResponse
import pytest

from app.services.platform.endpoints import design


class _FakeScalarListResult:
    def __init__(self, items):
        self._items = items

    class _ScalarAccessor:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    def scalars(self):
        return self._ScalarAccessor(self._items)


class _FakeSingleResult:
    def __init__(self, item):
        self._item = item

    def scalar_one_or_none(self):
        return self._item


class _FakeAsyncSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def execute(self, _statement):
        if not self._execute_values:
            raise AssertionError("Unexpected execute call")
        return self._execute_values.pop(0)


def _request(
    *,
    request_id: uuid.UUID,
    user_id: uuid.UUID,
    source: str,
    input_r2_key: str | None,
    input_filename: str | None,
):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=request_id,
        user_id=user_id,
        source=source,
        status="completed",
        input_upload_id=None,
        input_r2_key=input_r2_key,
        input_filename=input_filename,
        building_type="living-room",
        style_id="modern",
        palette_id="surprise-me",
        prompt="refresh",
        submitted_at=now,
        updated_at=now,
        output_preview_url="https://example.com/output.png",
        deleted_at=None,
    )


@pytest.mark.asyncio
async def test_list_my_design_requests_includes_example_source(monkeypatch):
    current_user_id = uuid.uuid4()
    upload_request = _request(
        request_id=uuid.uuid4(),
        user_id=current_user_id,
        source="upload",
        input_r2_key="u/key.webp",
        input_filename=None,
    )
    example_request = _request(
        request_id=uuid.uuid4(),
        user_id=current_user_id,
        source="example",
        input_r2_key="assets/3-4/discover-kitchen.webp",
        input_filename=None,
    )

    db = _FakeAsyncSession([_FakeScalarListResult([upload_request, example_request])])

    monkeypatch.setattr(design.settings, "r2_endpoint_url", "https://r2.example.com")

    response = await design.list_my_design_requests(
        current_user_id=current_user_id,
        db=db,
    )

    assert len(response.items) == 2
    sources = {item.source.value for item in response.items}
    assert sources == {"upload", "example"}


@pytest.mark.asyncio
async def test_get_design_request_preview_allows_example_source_with_r2(monkeypatch):
    current_user_id = uuid.uuid4()
    request_id = uuid.uuid4()
    example_request = _request(
        request_id=request_id,
        user_id=current_user_id,
        source="example",
        input_r2_key="assets/3-4/discover-kitchen.webp",
        input_filename=None,
    )

    db = _FakeAsyncSession([_FakeSingleResult(example_request)])

    monkeypatch.setattr(design.settings, "r2_endpoint_url", "https://r2.example.com")
    monkeypatch.setattr(
        design,
        "generate_presigned_url",
        lambda key: f"https://cdn.example.com/{key}",
    )

    response = await design.get_design_request_preview(
        design_request_id=request_id,
        current_user_id=current_user_id,
        db=db,
    )

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == "https://cdn.example.com/assets/3-4/discover-kitchen.webp"


@pytest.mark.asyncio
async def test_get_design_request_preview_allows_example_source_with_local_file(monkeypatch):
    current_user_id = uuid.uuid4()
    request_id = uuid.uuid4()
    example_request = _request(
        request_id=request_id,
        user_id=current_user_id,
        source="example",
        input_r2_key=None,
        input_filename="example-local.webp",
    )

    db = _FakeAsyncSession([_FakeSingleResult(example_request)])

    sample_preview_path = Path("/tmp/example-local.webp")
    monkeypatch.setattr(design.settings, "r2_endpoint_url", None)
    monkeypatch.setattr(
        design,
        "_resolve_preview_file_for_request",
        lambda user_id, design_request: sample_preview_path,
    )

    response = await design.get_design_request_preview(
        design_request_id=request_id,
        current_user_id=current_user_id,
        db=db,
    )

    assert str(response.path) == str(sample_preview_path)
