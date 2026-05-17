from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import IntegrityError

from app.services.platform.endpoints import saved
from app.services.platform.schemas.saved import (
    SaveDesignRequestItemRequest,
    SaveDiscoverCardItemRequest,
)


class _FakeResult:
    def __init__(
        self,
        *,
        scalar_value=None,
        scalars_list: list[object] | None = None,
        rows: list[tuple[object, object]] | None = None,
    ):
        self._scalar_value = scalar_value
        self._scalars_list = scalars_list if scalars_list is not None else []
        self._rows = rows if rows is not None else []

    class _ScalarAccessor:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalars(self):
        return self._ScalarAccessor(self._scalars_list)

    def all(self):
        return self._rows


class _FakeAsyncSession:
    def __init__(self, *, execute_results: list[_FakeResult] | None = None, commit_effects=None):
        self._execute_results = list(execute_results or [])
        self._commit_effects = list(commit_effects or [])
        self.added: list[object] = []
        self.commit_count = 0
        self.refresh_count = 0
        self.rollback_count = 0

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected execute call")
        return self._execute_results.pop(0)

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    async def commit(self):
        self.commit_count += 1
        if self._commit_effects:
            effect = self._commit_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect

    async def refresh(self, _value):
        self.refresh_count += 1

    async def rollback(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_save_discover_item_success(monkeypatch):
    user_id = uuid.uuid4()
    saved_item = SimpleNamespace(id=uuid.uuid4(), saved_at=datetime.now(UTC))
    discover_card = SimpleNamespace(
        card_id="kitchen-1",
        category_key="home",
        section_id="kitchen",
        section_title="Kitchen",
    )
    discover_asset = SimpleNamespace(r2_key="assets/3-4/kitchen.webp")

    async def _fake_load_discover_card(_db, *, discover_card_id):
        assert discover_card_id == "kitchen-1"
        return discover_card, discover_asset

    async def _fake_create_or_restore_saved_item(
        _db,
        *,
        current_user_id,
        item_type,
        discover_card_id=None,
        design_request_id=None,
    ):
        assert current_user_id == user_id
        assert item_type == saved.SavedItemType.DISCOVER_CARD
        assert discover_card_id == "kitchen-1"
        assert design_request_id is None
        return saved_item

    monkeypatch.setattr(saved.settings, "r2_public_url", "https://cdn.example.com")
    monkeypatch.setattr(saved, "_load_discover_card", _fake_load_discover_card)
    monkeypatch.setattr(saved, "_create_or_restore_saved_item", _fake_create_or_restore_saved_item)

    response = await saved.save_item(
        SaveDiscoverCardItemRequest(item_type="discover_card", discover_card_id="kitchen-1"),
        current_user_id=user_id,
        db=SimpleNamespace(),
    )

    assert response.saved_item_id == saved_item.id
    assert response.item_type == saved.SavedItemType.DISCOVER_CARD
    assert response.discover_card_id == "kitchen-1"
    assert response.section_title == "Kitchen"
    assert response.image_url == "https://cdn.example.com/assets/3-4/kitchen.webp"


@pytest.mark.asyncio
async def test_create_or_restore_saved_item_is_idempotent_for_active_row(monkeypatch):
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        deleted_at=None,
        saved_at=datetime.now(UTC),
    )
    db = _FakeAsyncSession()

    async def _fake_find_existing(*args, **kwargs):
        return existing

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_existing)

    response = await saved._create_or_restore_saved_item(
        db,
        current_user_id=uuid.uuid4(),
        item_type=saved.SavedItemType.DISCOVER_CARD,
        discover_card_id="living-room-1",
    )

    assert response is existing
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_create_or_restore_saved_item_restores_soft_deleted_row(monkeypatch):
    old_saved_at = datetime.now(UTC) - timedelta(days=1)
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        deleted_at=datetime.now(UTC),
        saved_at=old_saved_at,
    )
    db = _FakeAsyncSession()

    async def _fake_find_existing(*args, **kwargs):
        return existing

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_existing)

    response = await saved._create_or_restore_saved_item(
        db,
        current_user_id=uuid.uuid4(),
        item_type=saved.SavedItemType.DISCOVER_CARD,
        discover_card_id="living-room-1",
    )

    assert response is existing
    assert existing.deleted_at is None
    assert existing.saved_at > old_saved_at
    assert db.commit_count == 1
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_create_or_restore_saved_item_handles_insert_race(monkeypatch):
    restored = SimpleNamespace(
        id=uuid.uuid4(),
        deleted_at=datetime.now(UTC),
        saved_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db = _FakeAsyncSession(
        commit_effects=[IntegrityError("insert", {}, Exception("duplicate")), None],
    )
    find_calls: list[int] = []

    async def _fake_find_existing(*args, **kwargs):
        find_calls.append(1)
        if len(find_calls) == 1:
            return None
        return restored

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_existing)

    response = await saved._create_or_restore_saved_item(
        db,
        current_user_id=uuid.uuid4(),
        item_type=saved.SavedItemType.DESIGN_REQUEST,
        design_request_id=uuid.uuid4(),
    )

    assert response is restored
    assert restored.deleted_at is None
    assert db.rollback_count == 1
    assert db.commit_count == 2
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_unsave_item_is_idempotent(monkeypatch):
    db = _FakeAsyncSession()

    async def _fake_find_none(*args, **kwargs):
        return None

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_none)

    await saved.unsave_item(
        SaveDiscoverCardItemRequest(item_type="discover_card", discover_card_id="kitchen-1"),
        current_user_id=uuid.uuid4(),
        db=db,
    )
    assert db.commit_count == 0

    soft_deleted = SimpleNamespace(id=uuid.uuid4(), deleted_at=datetime.now(UTC))

    async def _fake_find_deleted(*args, **kwargs):
        return soft_deleted

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_deleted)
    await saved.unsave_item(
        SaveDiscoverCardItemRequest(item_type="discover_card", discover_card_id="kitchen-1"),
        current_user_id=uuid.uuid4(),
        db=db,
    )
    assert db.commit_count == 0

    active = SimpleNamespace(id=uuid.uuid4(), deleted_at=None)

    async def _fake_find_active(*args, **kwargs):
        return active

    monkeypatch.setattr(saved, "_find_existing_saved_item", _fake_find_active)
    await saved.unsave_item(
        SaveDiscoverCardItemRequest(item_type="discover_card", discover_card_id="kitchen-1"),
        current_user_id=uuid.uuid4(),
        db=db,
    )
    assert db.commit_count == 1
    assert active.deleted_at is not None


@pytest.mark.asyncio
async def test_load_completed_design_request_rejects_non_completed():
    request_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = _FakeAsyncSession(
        execute_results=[
            _FakeResult(
                scalar_value=SimpleNamespace(
                    id=request_id,
                    user_id=user_id,
                    status="processing",
                    deleted_at=None,
                )
            )
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await saved._load_completed_design_request(
            db,
            current_user_id=user_id,
            design_request_id=request_id,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Only completed design requests can be saved"


@pytest.mark.asyncio
async def test_load_completed_design_request_rejects_non_owner():
    db = _FakeAsyncSession(execute_results=[_FakeResult(scalar_value=None)])

    with pytest.raises(HTTPException) as exc_info:
        await saved._load_completed_design_request(
            db,
            current_user_id=uuid.uuid4(),
            design_request_id=uuid.uuid4(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Design request not found"


@pytest.mark.asyncio
async def test_list_saved_items_returns_mixed_shapes(monkeypatch):
    user_id = uuid.uuid4()
    design_request_id = uuid.uuid4()
    discover_saved_item = SimpleNamespace(
        id=uuid.uuid4(),
        item_type="discover_card",
        discover_card_id="kitchen-1",
        design_request_id=None,
        saved_at=datetime.now(UTC),
        deleted_at=None,
    )
    design_saved_item = SimpleNamespace(
        id=uuid.uuid4(),
        item_type="design_request",
        discover_card_id=None,
        design_request_id=design_request_id,
        saved_at=datetime.now(UTC) - timedelta(minutes=1),
        deleted_at=None,
    )
    db = _FakeAsyncSession(
        execute_results=[
            _FakeResult(scalars_list=[discover_saved_item, design_saved_item]),
            _FakeResult(
                scalar_value=SimpleNamespace(
                    id=design_request_id,
                    user_id=user_id,
                    status="completed",
                    building_type="living-room",
                    style_id="modern",
                    palette_id="surprise-me",
                    output_preview_url="https://example.com/output.png",
                    input_r2_key="uploads/input.webp",
                    input_filename=None,
                    deleted_at=None,
                )
            ),
        ]
    )
    discover_card = SimpleNamespace(
        card_id="kitchen-1",
        category_key="home",
        section_id="kitchen",
        section_title="Kitchen",
    )
    discover_asset = SimpleNamespace(r2_key="assets/3-4/kitchen.webp")

    async def _fake_load_discover_card(_db, *, discover_card_id):
        assert discover_card_id == "kitchen-1"
        return discover_card, discover_asset

    monkeypatch.setattr(saved, "_load_discover_card", _fake_load_discover_card)
    monkeypatch.setattr(saved.settings, "r2_public_url", "https://cdn.example.com")
    monkeypatch.setattr(saved.settings, "r2_endpoint_url", "https://r2.example.com")

    response = await saved.list_saved_items(current_user_id=user_id, db=db)

    assert len(response.items) == 2

    first = response.items[0]
    assert first.item_type == saved.SavedItemType.DISCOVER_CARD
    assert first.discover_card_id == "kitchen-1"
    assert first.category_key == "home"
    assert first.image_url == "https://cdn.example.com/assets/3-4/kitchen.webp"

    second = response.items[1]
    assert second.item_type == saved.SavedItemType.DESIGN_REQUEST
    assert second.design_request_id == design_request_id
    assert second.building_type == "living-room"
    assert second.preview_url == f"/api/v1/designs/{design_request_id}/preview"


@pytest.mark.asyncio
async def test_save_design_item_success(monkeypatch):
    user_id = uuid.uuid4()
    design_request_id = uuid.uuid4()
    saved_item = SimpleNamespace(id=uuid.uuid4(), saved_at=datetime.now(UTC))
    design_request = SimpleNamespace(
        id=design_request_id,
        user_id=user_id,
        status="completed",
        building_type="living-room",
        style_id="modern",
        palette_id="surprise-me",
        output_preview_url="https://example.com/output.png",
        input_r2_key="uploads/input.webp",
        input_filename=None,
        deleted_at=None,
    )

    async def _fake_load_completed_design_request(
        _db,
        *,
        current_user_id,
        design_request_id: uuid.UUID,
    ):
        assert current_user_id == user_id
        assert design_request_id == design_request.id
        return design_request

    async def _fake_create_or_restore_saved_item(
        _db,
        *,
        current_user_id,
        item_type,
        discover_card_id=None,
        design_request_id=None,
    ):
        assert current_user_id == user_id
        assert item_type == saved.SavedItemType.DESIGN_REQUEST
        assert discover_card_id is None
        assert design_request_id == design_request.id
        return saved_item

    monkeypatch.setattr(saved.settings, "r2_endpoint_url", "https://r2.example.com")
    monkeypatch.setattr(
        saved,
        "_load_completed_design_request",
        _fake_load_completed_design_request,
    )
    monkeypatch.setattr(saved, "_create_or_restore_saved_item", _fake_create_or_restore_saved_item)

    response = await saved.save_item(
        SaveDesignRequestItemRequest(
            item_type="design_request",
            design_request_id=design_request_id,
        ),
        current_user_id=user_id,
        db=SimpleNamespace(),
    )

    assert response.saved_item_id == saved_item.id
    assert response.item_type == saved.SavedItemType.DESIGN_REQUEST
    assert response.design_request_id == design_request.id
    assert response.preview_url == f"/api/v1/designs/{design_request.id}/preview"
