import uuid

from pydantic import BaseModel, Field, field_validator


class DeviceLoginRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=255)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("device_id cannot be empty")
        return normalized_value


class DeviceLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: uuid.UUID


class AuthMeResponse(BaseModel):
    user_id: uuid.UUID