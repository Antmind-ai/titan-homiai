from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import InvalidTokenError, create_access_token, decode_access_token
from app.services.platform.models import DeviceUser
from app.services.platform.schemas.auth import AuthMeResponse, DeviceLoginRequest, DeviceLoginResponse

router = APIRouter(prefix="/auth")

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


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

    result = await db.execute(select(DeviceUser).where(DeviceUser.device_id == payload.device_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = DeviceUser(device_id=payload.device_id, last_seen_at=now)
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            # Handle race condition where another request created the same device user.
            await db.rollback()
            result = await db.execute(
                select(DeviceUser).where(DeviceUser.device_id == payload.device_id)
            )
            user = result.scalar_one()
            user.last_seen_at = now
            db.add(user)
            await db.flush()
    else:
        user.last_seen_at = now
        await db.flush()

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
async def auth_me(current_user_id: uuid.UUID = Depends(get_current_user_id)) -> AuthMeResponse:
    return AuthMeResponse(user_id=current_user_id)