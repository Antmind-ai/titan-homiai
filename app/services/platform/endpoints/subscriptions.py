import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.credit_service import add_credits, get_credit_balance
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.models.subscription import PurchaseRecord
from app.services.platform.schemas.subscription import (
    RestoreResponse,
    SubscriptionMeResponse,
    SubscriptionProductResponse,
    SubscriptionProductsResponse,
)
from app.services.platform import revenuecat_api, revenuecat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions")


@router.get(
    "/products",
    response_model=SubscriptionProductsResponse,
    summary="List available subscription products",
)
async def list_products() -> SubscriptionProductsResponse:
    products = [
        SubscriptionProductResponse(
            product_id=settings.subscription_weekly_product_id,
            plan_type="weekly",
            credit_amount=settings.subscription_weekly_credits,
        ),
        SubscriptionProductResponse(
            product_id=settings.subscription_yearly_product_id,
            plan_type="yearly",
            credit_amount=settings.subscription_yearly_credits,
        ),
    ]
    return SubscriptionProductsResponse(products=products)


@router.get(
    "/me",
    response_model=SubscriptionMeResponse,
    summary="Get current user's subscription status and balance",
)
async def subscription_me(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionMeResponse:
    try:
        balance = await get_credit_balance(db, current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    result = await db.execute(
        select(PurchaseRecord)
        .where(PurchaseRecord.user_id == current_user_id)
        .where(PurchaseRecord.is_active_subscription.is_(True))
        .order_by(PurchaseRecord.created_at.desc())
        .limit(1)
    )
    latest_purchase = result.scalar_one_or_none()

    if latest_purchase is None:
        return SubscriptionMeResponse(
            user_id=current_user_id,
            has_active_subscription=False,
            balance=balance,
        )

    plan_type = (
        "weekly"
        if latest_purchase.revenuecat_product_id == settings.subscription_weekly_product_id
        else "yearly"
        if latest_purchase.revenuecat_product_id == settings.subscription_yearly_product_id
        else "unknown"
    )

    credit_amount = (
        settings.subscription_weekly_credits
        if plan_type == "weekly"
        else settings.subscription_yearly_credits
    )

    return SubscriptionMeResponse(
        user_id=current_user_id,
        has_active_subscription=True,
        product_id=latest_purchase.revenuecat_product_id,
        plan_type=plan_type,
        credit_amount=credit_amount,
        expires_at=latest_purchase.expires_at.isoformat() if latest_purchase.expires_at else None,
        balance=balance,
    )


@router.post(
    "/restore",
    response_model=RestoreResponse,
    summary="Restore purchase by syncing with RevenueCat API",
)
async def restore_purchase(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> RestoreResponse:
    if not settings.revenuecat_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RevenueCat API key not configured",
        )

    try:
        balance = await get_credit_balance(db, current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    try:
        subscriber_data = await revenuecat_api.fetch_subscriber(str(current_user_id))
    except Exception as exc:
        logger.error("Failed to fetch subscriber from RevenueCat: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to sync with RevenueCat",
        )

    subscriber = subscriber_data.get("subscriber", {})
    if not subscriber:
        return RestoreResponse(
            user_id=current_user_id,
            has_active_subscription=False,
            balance=balance,
        )

    entitlements = revenuecat_api.extract_active_entitlements(subscriber)
    ent_id = "RoomAI Pro"
    active_ent = entitlements.get(ent_id)

    if not active_ent:
        return RestoreResponse(
            user_id=current_user_id,
            has_active_subscription=False,
            balance=balance,
        )

    purchase_info = revenuecat_api.extract_latest_purchase(subscriber, ent_id)
    if not purchase_info:
        return RestoreResponse(
            user_id=current_user_id,
            has_active_subscription=False,
            balance=balance,
        )

    product_id = purchase_info["product_id"]
    expires_at = revenuecat_api.parse_iso_datetime(purchase_info["expires_date"])
    purchased_at = revenuecat_api.parse_iso_datetime(purchase_info["purchase_date"])

    plan_type = (
        "weekly"
        if product_id == settings.subscription_weekly_product_id
        else "yearly"
        if product_id == settings.subscription_yearly_product_id
        else "unknown"
    )
    credit_amount = (
        settings.subscription_weekly_credits
        if plan_type == "weekly"
        else settings.subscription_yearly_credits
        if plan_type == "yearly"
        else 0
    )

    event_id = f"restore:{str(current_user_id)}:{product_id}"
    is_duplicate = await revenuecat_service.is_duplicate_event(db, event_id)

    credits_granted = 0
    if not is_duplicate:
        if credit_amount > 0:
            try:
                result = await add_credits(
                    db,
                    user_id=current_user_id,
                    credits=credit_amount,
                    source="subscription_restore",
                    reason=f"Restore: {product_id}",
                    reference_id=event_id,
                    idempotency_key=event_id,
                )
                credits_granted = result.applied_delta
            except Exception as exc:
                logger.warning("Failed to grant credits during restore: %s", exc)

        await revenuecat_service.record_purchase_event(
            db,
            user_id=current_user_id,
            event_id=event_id,
            event_type="RESTORE",
            product_id=product_id,
            transaction_id=event_id,
            environment=settings.app_environment,
            credit_amount=credit_amount,
            is_active=True,
            purchased_at=purchased_at,
            expires_at=expires_at,
            raw_payload=subscriber_data,
        )
        await db.commit()

    new_balance = balance + credits_granted

    return RestoreResponse(
        user_id=current_user_id,
        has_active_subscription=True,
        product_id=product_id,
        plan_type=plan_type,
        credit_amount=credit_amount,
        expires_at=expires_at.isoformat() if expires_at else None,
        balance=new_balance,
        credits_granted=credits_granted,
    )
