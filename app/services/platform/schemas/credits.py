from datetime import datetime
import uuid

from pydantic import BaseModel, Field, field_validator


class CreditsMeResponse(BaseModel):
    user_id: uuid.UUID
    balance: int = Field(..., ge=0)
    lifetime_free_credits: int = Field(..., ge=0)


class AddCreditsInternalRequest(BaseModel):
    user_id: uuid.UUID
    credits: int = Field(..., ge=1, le=100000)
    source: str = Field(default="internal", min_length=1, max_length=50)
    reason: str | None = Field(default=None, max_length=255)
    reference_id: str | None = Field(default=None, max_length=120)
    idempotency_key: str | None = Field(default=None, max_length=120)

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        normalized = value.strip().lower().replace(" ", "_")
        if not normalized:
            raise ValueError("source cannot be empty")
        return normalized


class CreditsMutationResponse(BaseModel):
    event_id: uuid.UUID
    user_id: uuid.UUID
    balance: int = Field(..., ge=0)
    applied_delta: int
    idempotent: bool
    source: str
    created_at: datetime


class InsufficientCreditsDetail(BaseModel):
    error: str = "insufficient_credits"
    message: str
    balance: int = Field(..., ge=0)
    required_credits: int = Field(..., ge=1)
    lifetime_free_credits: int = Field(..., ge=0)
