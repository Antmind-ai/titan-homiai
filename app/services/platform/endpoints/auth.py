from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import InvalidTokenError, create_access_token, decode_access_token
from app.services.platform import revenuecat_api, revenuecat_service
from app.services.platform.models import CreditLedgerEvent, DeviceCreditGrant, DeviceUser
from app.services.platform.schemas.auth import (
    AuthMeResponse,
    DeleteAccountResponse,
    DeviceLoginRequest,
    DeviceLoginResponse,
    MarkOnboardingCompletedResponse,
)

router = APIRouter(prefix="/auth")

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _load_user_by_device_id(
    db: AsyncSession,
    *,
    device_id: str,
) -> DeviceUser | None:
    result = await db.execute(select(DeviceUser).where(DeviceUser.device_id == device_id))
    return result.scalar_one_or_none()


async def _touch_active_user(
    db: AsyncSession,
    *,
    user: DeviceUser,
    now: datetime,
) -> DeviceUser:
    user.last_seen_at = now
    db.add(user)
    await db.flush()
    return user


async def _purge_deleted_user_with_device_id(
    db: AsyncSession,
    *,
    user: DeviceUser,
) -> None:
    await db.delete(user)
    await db.flush()


async def _claim_device_credit_grant(
    db: AsyncSession,
    *,
    device_id: str,
    first_user_id: uuid.UUID,
    credits: int,
) -> bool:
    grant_id = uuid.uuid4()
    statement = (
        pg_insert(DeviceCreditGrant)
        .values(
            id=grant_id,
            device_id=device_id,
            first_user_id=first_user_id,
            credits_granted=credits,
        )
        .on_conflict_do_nothing(index_elements=[DeviceCreditGrant.device_id])
        .returning(DeviceCreditGrant.id)
    )

    result = await db.execute(statement)
    return result.scalar_one_or_none() is not None


async def _create_user_with_device_lifetime_grant(
    db: AsyncSession,
    *,
    device_id: str,
    now: datetime,
) -> DeviceUser:
    initial_credits = settings.free_lifetime_credits
    user = DeviceUser(
        device_id=device_id,
        last_seen_at=now,
        credit_balance=0,
    )
    db.add(user)
    await db.flush()

    grant_claimed = await _claim_device_credit_grant(
        db,
        device_id=device_id,
        first_user_id=user.id,
        credits=initial_credits,
    )

    if grant_claimed and initial_credits > 0:
        user.credit_balance = initial_credits
        db.add(
            CreditLedgerEvent(
                user_id=user.id,
                delta=initial_credits,
                balance_after=initial_credits,
                source="signup_grant",
                reason="Initial free lifetime credits",
            )
        )
        await db.flush()

    return user


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> uuid.UUID:
    if credentials is None:
        raise _unauthorized_error()

    try:
        payload = decode_access_token(credentials.credentials)
        subject = payload.get("sub")
        if subject is None:
            raise ValueError("subject missing")
        return uuid.UUID(str(subject))
    except (InvalidTokenError, ValueError) as exc:
        raise _unauthorized_error() from exc


@router.post(
    "/device/login",
    response_model=DeviceLoginResponse,
    summary="Login or register with a device identifier",
)
async def device_login(
    payload: DeviceLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> DeviceLoginResponse:
    now = datetime.now(UTC)

    user = await _load_user_by_device_id(db, device_id=payload.device_id)

    if user is not None and user.deleted_at is not None:
        await _purge_deleted_user_with_device_id(db, user=user)
        user = None

    if user is None:
        try:
            user = await _create_user_with_device_lifetime_grant(
                db,
                device_id=payload.device_id,
                now=now,
            )
        except IntegrityError:
            # The device_id already exists because of a concurrent login.
            # Reuse that row and do not grant credits again.
            await db.rollback()
            user = await _load_user_by_device_id(db, device_id=payload.device_id)
            if user is None:
                raise
            if user.deleted_at is not None:
                await _purge_deleted_user_with_device_id(db, user=user)
                user = await _create_user_with_device_lifetime_grant(
                    db,
                    device_id=payload.device_id,
                    now=now,
                )
            else:
                user = await _touch_active_user(db, user=user, now=now)
    else:
        user = await _touch_active_user(db, user=user, now=now)

    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(subject=str(user.id))
    return DeviceLoginResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        user_id=user.id,
    )


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="Return current authenticated user",
)
async def auth_me(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> AuthMeResponse:
    result = await db.execute(
        select(DeviceUser.id, DeviceUser.onboarding_completed).where(
            DeviceUser.id == current_user_id,
            DeviceUser.deleted_at.is_(None),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return AuthMeResponse(user_id=row.id, onboarding_completed=row.onboarding_completed)


@router.patch(
    "/me/onboarding",
    response_model=MarkOnboardingCompletedResponse,
    summary="Mark onboarding as completed for the current user",
)
async def mark_onboarding_completed(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> MarkOnboardingCompletedResponse:
    result = await db.execute(
        select(DeviceUser).where(
            DeviceUser.id == current_user_id,
            DeviceUser.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user.onboarding_completed = True
    await db.commit()

    return MarkOnboardingCompletedResponse(user_id=current_user_id)


@router.delete(
    "/me",
    response_model=DeleteAccountResponse,
    summary="Delete the current user's account and all associated data",
)
async def delete_account(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DeleteAccountResponse:
    result = await db.execute(
        select(DeviceUser).where(
            DeviceUser.id == current_user_id,
            DeviceUser.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found or already deleted",
        )

    active_subscription = await revenuecat_service.get_current_subscription_record(
        db,
        user_id=current_user_id,
    )

    if active_subscription and settings.revenuecat_api_key:
        try:
            await revenuecat_api.cancel_subscription(
                app_user_id=str(current_user_id),
                product_identifier=active_subscription.revenuecat_product_id,
            )
            logger.info(
                "Cancelled subscription for user %s: product=%s",
                str(current_user_id),
                active_subscription.revenuecat_product_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to cancel subscription for user %s: %s",
                str(current_user_id),
                exc,
            )

    now = datetime.now(UTC)
    if user.credit_balance > 0:
        credits_to_forfeit = int(user.credit_balance)
        db.add(
            CreditLedgerEvent(
                user_id=user.id,
                delta=-credits_to_forfeit,
                balance_after=0,
                source="account_deletion_forfeit",
                reason="Account deletion forfeited remaining credits",
            )
        )
        user.credit_balance = 0

    await db.delete(user)
    await db.commit()

    from app.workers.client import enqueue_job

    job_id = await enqueue_job("cleanup_user_data_task", str(current_user_id))

    return DeleteAccountResponse(
        user_id=current_user_id,
        deleted_at=now,
        job_id=job_id,
    )
