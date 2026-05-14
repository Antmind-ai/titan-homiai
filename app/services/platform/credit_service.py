from dataclasses import dataclass
from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.platform.models import CreditLedgerEvent, DeviceUser


@dataclass(frozen=True)
class CreditMutationResult:
    event_id: uuid.UUID
    user_id: uuid.UUID
    balance: int
    applied_delta: int
    idempotent: bool
    source: str
    created_at: datetime


class InsufficientCreditsError(Exception):
    def __init__(self, balance: int, required_credits: int = 25):
        super().__init__("Insufficient credits")
        self.balance = balance
        self.required_credits = required_credits


async def get_credit_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(DeviceUser.credit_balance).where(
            DeviceUser.id == user_id,
            DeviceUser.deleted_at.is_(None),
        )
    )
    balance = result.scalar_one_or_none()
    if balance is None:
        raise ValueError("User not found")
    return int(balance)


async def consume_credit(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    source: str,
    reason: str | None = None,
    reference_id: str | None = None,
    credits: int = 25,
) -> CreditMutationResult:
    if credits < 25:
        raise ValueError("credits must be at least 25")
    return await apply_credit_delta(
        db,
        user_id=user_id,
        delta=-credits,
        source=source,
        reason=reason,
        reference_id=reference_id,
    )


async def add_credits(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    credits: int,
    source: str,
    reason: str | None = None,
    reference_id: str | None = None,
    idempotency_key: str | None = None,
) -> CreditMutationResult:
    if credits <= 0:
        raise ValueError("credits must be positive")

    return await apply_credit_delta(
        db,
        user_id=user_id,
        delta=credits,
        source=source,
        reason=reason,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
    )


async def apply_credit_delta(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    delta: int,
    source: str,
    reason: str | None = None,
    reference_id: str | None = None,
    idempotency_key: str | None = None,
) -> CreditMutationResult:
    if delta == 0:
        raise ValueError("delta cannot be zero")

    user = await _lock_user(db, user_id=user_id)

    if idempotency_key:
        existing_event = await _find_event_by_idempotency_key(
            db,
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if existing_event is not None:
            return _result_from_event(existing_event, idempotent=True)

    next_balance = user.credit_balance + delta
    if next_balance < 0:
        raise InsufficientCreditsError(
            balance=user.credit_balance,
            required_credits=abs(delta),
        )

    user.credit_balance = next_balance

    event = CreditLedgerEvent(
        user_id=user.id,
        delta=delta,
        balance_after=next_balance,
        source=source,
        reason=reason,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    await db.flush()

    return _result_from_event(event, idempotent=False)


async def _lock_user(db: AsyncSession, *, user_id: uuid.UUID) -> DeviceUser:
    result = await db.execute(
        select(DeviceUser).where(
            DeviceUser.id == user_id,
            DeviceUser.deleted_at.is_(None),
        ).with_for_update()
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError("User not found")
    return user


async def _find_event_by_idempotency_key(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    idempotency_key: str,
) -> CreditLedgerEvent | None:
    result = await db.execute(
        select(CreditLedgerEvent)
        .where(CreditLedgerEvent.user_id == user_id)
        .where(CreditLedgerEvent.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


def _result_from_event(event: CreditLedgerEvent, *, idempotent: bool) -> CreditMutationResult:
    created_at = event.created_at if event.created_at is not None else datetime.now(UTC)
    return CreditMutationResult(
        event_id=event.id,
        user_id=event.user_id,
        balance=event.balance_after,
        applied_delta=event.delta,
        idempotent=idempotent,
        source=event.source,
        created_at=created_at,
    )
