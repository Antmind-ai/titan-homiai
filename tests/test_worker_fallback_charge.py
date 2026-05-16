from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.services.platform.credit_service import InsufficientCreditsError
from app.workers import tasks


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, design_request):
        self.design_request = design_request
        self.commit_count = 0

    async def execute(self, _statement):
        return _FakeResult(self.design_request)

    async def commit(self):
        self.commit_count += 1


class _FakeSequentialDB:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def execute(self, _statement):
        if not self._execute_values:
            raise AssertionError("Unexpected execute call")
        return _FakeResult(self._execute_values.pop(0))


@pytest.mark.asyncio
async def test_process_design_request_task_fails_when_fallback_charge_cannot_be_collected(
    monkeypatch,
):
    request_id = uuid.uuid4()
    user_id = uuid.uuid4()
    design_request = SimpleNamespace(
        id=request_id,
        user_id=user_id,
        prompt="refresh the room",
        building_type="living-room",
        style_id="modern",
        palette_id="surprise-me",
        status="queued",
        processing_started_at=None,
        failed_at=None,
        error_message=None,
        completed_at=None,
        output_preview_url=None,
    )

    db_instances = [_FakeDB(design_request), _FakeDB(design_request), _FakeDB(design_request)]

    @asynccontextmanager
    async def _fake_get_db_context():
        if not db_instances:
            raise AssertionError("Unexpected DB context request")
        yield db_instances.pop(0)

    async def _fake_resolve_input_image_path(_design_request):
        return Path("tests/mock-input.jpg"), False

    async def _fake_generate_image(*, prompt: str, image_path: str):
        assert "living-room" in prompt
        assert image_path == "tests/mock-input.jpg"
        return SimpleNamespace(
            url="https://example.com/output.png",
            model="fal-ai/nano-banana-pro/edit",
        )

    async def _fake_consume_credit(*args, **kwargs):
        raise InsufficientCreditsError(balance=0, required_credits=50)

    refund_calls: list[str] = []

    async def _fake_refund_design_request_credit(db, *, design_request, failure_reason):
        assert db is not None
        assert str(design_request.id) == str(request_id)
        refund_calls.append(failure_reason)
        return True

    monkeypatch.setattr(tasks, "get_db_context", _fake_get_db_context)
    monkeypatch.setattr(tasks, "_resolve_input_image_path", _fake_resolve_input_image_path)
    monkeypatch.setattr(tasks, "generate_image", _fake_generate_image)
    monkeypatch.setattr(tasks, "get_model_credit_cost", lambda _model: 75)
    monkeypatch.setattr(tasks, "consume_credit", _fake_consume_credit)
    monkeypatch.setattr(tasks, "_refund_design_request_credit", _fake_refund_design_request_credit)

    result = await tasks.process_design_request_task({}, str(request_id))

    assert result["status"] == "failed"
    assert design_request.status == "failed"
    assert design_request.output_preview_url is None
    assert design_request.completed_at is None
    assert design_request.failed_at is not None
    assert "additional credits" in design_request.error_message.lower()
    assert len(refund_calls) == 1


@pytest.mark.asyncio
async def test_process_design_request_task_restores_credits_when_generation_fails(
    monkeypatch,
):
    request_id = uuid.uuid4()
    user_id = uuid.uuid4()
    design_request = SimpleNamespace(
        id=request_id,
        user_id=user_id,
        source="upload",
        example_photo_id=None,
        prompt="refresh the room",
        building_type="living-room",
        style_id="modern",
        palette_id="surprise-me",
        status="queued",
        processing_started_at=None,
        failed_at=None,
        error_message=None,
        completed_at=None,
        output_preview_url=None,
    )

    db_instances = [_FakeDB(design_request), _FakeDB(design_request)]

    @asynccontextmanager
    async def _fake_get_db_context():
        if not db_instances:
            raise AssertionError("Unexpected DB context request")
        yield db_instances.pop(0)

    async def _fake_resolve_input_image_path(_design_request):
        return Path("tests/mock-input.jpg"), False

    async def _fake_generate_image(*, prompt: str, image_path: str):
        assert "living-room" in prompt
        assert image_path == "tests/mock-input.jpg"
        raise tasks.DesignGenerationError("fal.ai generation request failed: timeout")

    refund_calls: list[str] = []

    async def _fake_refund_design_request_credit(db, *, design_request, failure_reason):
        assert db is not None
        assert str(design_request.id) == str(request_id)
        refund_calls.append(failure_reason)
        return True

    monkeypatch.setattr(tasks, "get_db_context", _fake_get_db_context)
    monkeypatch.setattr(tasks, "_resolve_input_image_path", _fake_resolve_input_image_path)
    monkeypatch.setattr(tasks, "generate_image", _fake_generate_image)
    monkeypatch.setattr(tasks, "_refund_design_request_credit", _fake_refund_design_request_credit)

    result = await tasks.process_design_request_task({}, str(request_id))

    assert result["status"] == "failed"
    assert design_request.status == "failed"
    assert design_request.failed_at is not None
    assert "fal.ai generation request failed" in design_request.error_message
    assert len(refund_calls) == 1


@pytest.mark.asyncio
async def test_refund_design_request_credit_uses_original_charge_amount(monkeypatch):
    request_id = uuid.uuid4()
    user_id = uuid.uuid4()
    design_request = SimpleNamespace(id=request_id, user_id=user_id)
    original_charge_event = SimpleNamespace(delta=-25)
    db = _FakeSequentialDB([original_charge_event])

    observed: dict[str, object] = {}

    async def _fake_add_credits(
        db,
        user_id,
        *,
        credits,
        source,
        reason,
        reference_id,
        idempotency_key,
    ):
        observed["db"] = db
        observed["user_id"] = user_id
        observed["credits"] = credits
        observed["source"] = source
        observed["reason"] = reason
        observed["reference_id"] = reference_id
        observed["idempotency_key"] = idempotency_key
        return SimpleNamespace(idempotent=False, balance=125)

    monkeypatch.setattr(tasks, "add_credits", _fake_add_credits)

    restored = await tasks._refund_design_request_credit(
        db,
        design_request=design_request,
        failure_reason="fal.ai generation request failed: timeout",
    )

    assert restored is True
    assert observed["db"] is db
    assert observed["user_id"] == user_id
    assert observed["credits"] == 25
    assert observed["source"] == "design_request_refund"
    assert observed["reference_id"] == str(request_id)
    assert observed["idempotency_key"] == f"design-request-refund:{request_id}"
    assert "Design generation failed:" in observed["reason"]


@pytest.mark.asyncio
async def test_refund_design_request_credit_skips_when_original_charge_missing(monkeypatch):
    request_id = uuid.uuid4()
    user_id = uuid.uuid4()
    design_request = SimpleNamespace(id=request_id, user_id=user_id)
    db = _FakeSequentialDB([None])

    async def _unexpected_add_credits(*args, **kwargs):
        raise AssertionError("add_credits should not be called without original charge")

    monkeypatch.setattr(tasks, "add_credits", _unexpected_add_credits)

    restored = await tasks._refund_design_request_credit(
        db,
        design_request=design_request,
        failure_reason="fal.ai generation request failed: timeout",
    )

    assert restored is False


@pytest.mark.asyncio
async def test_resolve_input_image_path_missing_inputs_includes_source_context():
    design_request = SimpleNamespace(
        source="example",
        example_photo_id="kitchen-1",
        input_r2_key=None,
        input_filename=None,
    )

    with pytest.raises(tasks.DesignGenerationError) as exc_info:
        await tasks._resolve_input_image_path(design_request)

    message = str(exc_info.value)
    assert "source=example" in message
    assert "example_photo_id=kitchen-1" in message
