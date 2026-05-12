import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

REVENUECAT_API_BASE = "https://api.revenuecat.com"


def _build_headers() -> dict[str, str]:
    if not settings.revenuecat_api_key:
        raise RuntimeError("RevenueCat API key is not configured")
    return {
        "Authorization": f"Bearer {settings.revenuecat_api_key}",
        "Content-Type": "application/json",
    }


async def fetch_subscriber(app_user_id: str) -> dict[str, Any]:
    url = f"{REVENUECAT_API_BASE}/v1/subscribers/{app_user_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=_build_headers())
        if response.status_code == 404:
            return {"subscriber": {}}
        response.raise_for_status()
        return response.json()


def extract_active_entitlements(
    subscriber: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    entitlements = subscriber.get("entitlements", {})
    return {
        name: info
        for name, info in entitlements.items()
        if info.get("expires_date") is not None
    }


def extract_latest_purchase(
    subscriber: dict[str, Any],
    entitlement_id: str,
) -> dict[str, Any] | None:
    entitlements = subscriber.get("entitlements", {})
    ent = entitlements.get(entitlement_id)
    if not ent:
        return None
    product_id = ent.get("product_identifier", "")
    purchase_date = ent.get("purchase_date")
    expires_date = ent.get("expires_date")
    return {
        "product_id": product_id,
        "purchase_date": purchase_date,
        "expires_date": expires_date,
    }


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
