import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.credit_service import add_credits, get_credit_balance
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.schemas.credits import (
    AddCreditsInternalRequest,
    CreditsMeResponse,
    CreditsMutationResponse,
)

router = APIRouter(prefix="/credits")


@router.get(
    "/me",
    response_model=CreditsMeResponse,
    summary="Return current authenticated user's credits",
)
async def credits_me(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> CreditsMeResponse:
    try:
        balance = await get_credit_balance(db, current_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc

    return CreditsMeResponse(
        user_id=current_user_id,
        balance=balance,
        lifetime_free_credits=settings.free_lifetime_credits,
    )


@router.post(
    "/internal/add",
    response_model=CreditsMutationResponse,
    summary="Add credits using internal service credentials",
)
async def add_credits_internal(
    payload: AddCreditsInternalRequest,
    db: AsyncSession = Depends(get_db),
    x_service_secret: str | None = Header(default=None, alias="X-Service-Secret"),
) -> CreditsMutationResponse:
    _authorize_internal_request(x_service_secret)

    try:
        mutation = await add_credits(
            db,
            user_id=payload.user_id,
            credits=payload.credits,
            source=payload.source,
            reason=payload.reason,
            reference_id=payload.reference_id,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc

    await db.commit()

    return CreditsMutationResponse(
        event_id=mutation.event_id,
        user_id=mutation.user_id,
        balance=mutation.balance,
        applied_delta=mutation.applied_delta,
        idempotent=mutation.idempotent,
        source=mutation.source,
        created_at=mutation.created_at,
    )


@router.post(
    "/self-topup",
    response_model=CreditsMutationResponse,
    summary="Temporarily add test credits for the current user",
)
async def add_credits_self_topup(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> CreditsMutationResponse:
    if not _is_self_topup_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Temporary self-topup is disabled",
        )

    try:
        mutation = await add_credits(
            db,
            user_id=current_user_id,
            credits=settings.credit_self_topup_amount,
            source="self_topup",
            reason="Temporary testing topup",
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc

    await db.commit()

    return CreditsMutationResponse(
        event_id=mutation.event_id,
        user_id=mutation.user_id,
        balance=mutation.balance,
        applied_delta=mutation.applied_delta,
        idempotent=mutation.idempotent,
        source=mutation.source,
        created_at=mutation.created_at,
    )


def _authorize_internal_request(service_secret: str | None) -> None:
    configured_secret = settings.credits_internal_api_key
    if not configured_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal credits API key is not configured",
        )

    if service_secret is None or not secrets.compare_digest(service_secret, configured_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal service credentials",
        )


def _is_self_topup_enabled() -> bool:
    if not settings.enable_credit_self_topup:
        return False

    normalized_environment = settings.app_environment.strip().lower()
    return normalized_environment in {"dev", "development", "local"}
