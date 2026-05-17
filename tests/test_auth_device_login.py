from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.platform.endpoints import auth
from app.services.platform.models.credit import CreditLedgerEvent
from app.services.platform.models.user import DeviceUser
from app.services.platform.schemas.auth import DeviceLoginRequest


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            raise AssertionError("Expected a scalar value")
        return self._value

    def one_or_none(self):
        return self._value


class _FakeAsyncSession:
    def __init__(self, *, execute_results, flush_effects=None):
        self._execute_results = list(execute_results)
        self._flush_effects = list(flush_effects or [])
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commit_count = 0
        self.refresh_count = 0
        self.rollback_count = 0

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected execute call")
        return _FakeResult(self._execute_results.pop(0))

    def add(self, value):
        if hasattr(value, "id") and getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    async def flush(self):
        if not self._flush_effects:
            return
        effect = self._flush_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, _value):
        self.refresh_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def delete(self, value):
        self.deleted.append(value)


@pytest.mark.asyncio
async def test_device_login_new_device_grants_twenty_five_credits(monkeypatch):
    grant_id = uuid.uuid4()
    db = _FakeAsyncSession(
        execute_results=[None, grant_id],
        flush_effects=[None, None],
    )

    monkeypatch.setattr(auth.settings, "free_lifetime_credits", 25)
    monkeypatch.setattr(auth, "create_access_token", lambda subject: f"token-for:{subject}")

    response = await auth.device_login(DeviceLoginRequest(device_id="device-123"), db)

    created_user = next(item for item in db.added if isinstance(item, DeviceUser))
    signup_events = [
        item
        for item in db.added
        if isinstance(item, CreditLedgerEvent) and item.source == "signup_grant"
    ]

    assert response.user_id == created_user.id
    assert created_user.credit_balance == 25
    assert len(signup_events) == 1
    assert signup_events[0].delta == 25
    assert signup_events[0].balance_after == 25
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_device_login_existing_active_device_does_not_grant_credits(monkeypatch):
    user_id = uuid.uuid4()
    now_before = datetime.now(UTC)
    existing_user = SimpleNamespace(
        id=user_id,
        device_id="device-123",
        deleted_at=None,
        credit_balance=7,
        onboarding_completed=True,
        last_seen_at=None,
    )
    db = _FakeAsyncSession(execute_results=[existing_user])

    monkeypatch.setattr(auth, "create_access_token", lambda subject: f"token-for:{subject}")

    response = await auth.device_login(DeviceLoginRequest(device_id="device-123"), db)

    assert response.user_id == user_id
    assert existing_user.credit_balance == 7
    assert existing_user.onboarding_completed is True
    assert existing_user.last_seen_at is not None
    assert existing_user.last_seen_at >= now_before
    assert not any(isinstance(item, CreditLedgerEvent) for item in db.added)


@pytest.mark.asyncio
async def test_device_login_deleted_user_creates_new_user(monkeypatch):
    deleted_user_id = uuid.uuid4()
    deleted_user = SimpleNamespace(
        id=deleted_user_id,
        device_id="device-123",
        deleted_at=datetime(2026, 5, 1, tzinfo=UTC),
        credit_balance=0,
        onboarding_completed=True,
        last_seen_at=None,
    )
    db = _FakeAsyncSession(execute_results=[deleted_user, None])

    monkeypatch.setattr(auth, "create_access_token", lambda subject: f"token-for:{subject}")

    response = await auth.device_login(DeviceLoginRequest(device_id="device-123"), db)

    assert response.user_id != deleted_user_id
    assert response.access_token == f"token-for:{response.user_id}"
    assert deleted_user in db.deleted
    assert db.rollback_count == 0
    assert not any(isinstance(item, CreditLedgerEvent) for item in db.added)


@pytest.mark.asyncio
async def test_device_login_new_user_with_existing_device_grant_starts_at_zero(
    monkeypatch,
):
    db = _FakeAsyncSession(
        execute_results=[None, None],
        flush_effects=[None],
    )

    monkeypatch.setattr(auth.settings, "free_lifetime_credits", 25)
    monkeypatch.setattr(auth, "create_access_token", lambda subject: f"token-for:{subject}")

    response = await auth.device_login(DeviceLoginRequest(device_id="device-123"), db)

    created_user = next(item for item in db.added if isinstance(item, DeviceUser))
    assert response.user_id == created_user.id
    assert created_user.credit_balance == 0
    assert not any(isinstance(item, CreditLedgerEvent) for item in db.added)


@pytest.mark.asyncio
async def test_device_login_concurrent_duplicate_device_does_not_grant_credits(
    monkeypatch,
):
    user_id = uuid.uuid4()
    existing_user = SimpleNamespace(
        id=user_id,
        device_id="device-123",
        deleted_at=None,
        credit_balance=25,
        onboarding_completed=True,
        last_seen_at=None,
    )
    db = _FakeAsyncSession(
        execute_results=[None, existing_user],
        flush_effects=[IntegrityError("insert", {}, Exception("duplicate"))],
    )

    monkeypatch.setattr(auth, "create_access_token", lambda subject: f"token-for:{subject}")

    response = await auth.device_login(DeviceLoginRequest(device_id="device-123"), db)

    assert response.user_id == user_id
    assert existing_user.credit_balance == 25
    assert db.rollback_count == 1
    assert not any(isinstance(item, CreditLedgerEvent) for item in db.added)


@pytest.mark.asyncio
async def test_delete_account_forfeits_remaining_credits(monkeypatch):
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        deleted_at=None,
        credit_balance=17,
    )
    db = _FakeAsyncSession(execute_results=[user])

    async def _no_subscription(*args, **kwargs):
        return None

    async def _fake_enqueue_job(function_name, *args, **kwargs):
        assert function_name == "cleanup_user_data_task"
        assert args == (str(user_id),)
        return "cleanup-job-123"

    monkeypatch.setattr(
        auth.revenuecat_service,
        "get_current_subscription_record",
        _no_subscription,
    )
    monkeypatch.setattr("app.workers.client.enqueue_job", _fake_enqueue_job)

    response = await auth.delete_account(current_user_id=user_id, db=db)

    forfeiture_events = [
        item
        for item in db.added
        if isinstance(item, CreditLedgerEvent) and item.source == "account_deletion_forfeit"
    ]
    assert response.user_id == user_id
    assert response.job_id == "cleanup-job-123"
    assert user.credit_balance == 0
    assert user in db.deleted
    assert len(forfeiture_events) == 1
    assert forfeiture_events[0].delta == -17
    assert forfeiture_events[0].balance_after == 0
