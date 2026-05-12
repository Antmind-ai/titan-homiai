import uuid

from pydantic import BaseModel, Field


class SubscriptionProductResponse(BaseModel):
    product_id: str
    plan_type: str
    credit_amount: int


class SubscriptionProductsResponse(BaseModel):
    products: list[SubscriptionProductResponse]


class RestoreResponse(BaseModel):
    user_id: uuid.UUID
    has_active_subscription: bool
    product_id: str | None = None
    plan_type: str | None = None
    credit_amount: int | None = None
    expires_at: str | None = None
    balance: int
    credits_granted: int = 0


class SubscriptionMeResponse(BaseModel):
    user_id: uuid.UUID
    has_active_subscription: bool
    product_id: str | None = None
    plan_type: str | None = None
    credit_amount: int | None = None
    expires_at: str | None = None
    balance: int
