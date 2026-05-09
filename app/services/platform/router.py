from fastapi import APIRouter

from app.services.platform.endpoints import auth
from app.services.platform.endpoints import health

router = APIRouter()

router.include_router(auth.router, tags=["Auth"])
router.include_router(health.router, tags=["Platform"])
