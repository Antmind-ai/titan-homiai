from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.platform.endpoints.auth import get_current_user_id
from app.services.platform.models import DesignRequest, DiscoverAsset, DiscoverCard, SavedItem
from app.services.platform.schemas.saved import (
    SavedItemMutationRequest,
    SavedItemResponse,
    SavedItemsListResponse,
    SavedItemType,
)

router = APIRouter(prefix="/saved")


def _build_public_r2_url(r2_key: str) -> str | None:
    base_url = (settings.r2_public_url or "").rstrip("/")
    if not base_url:
        return None

    normalized_key = r2_key.strip("/")
    encoded_key = "/".join(quote(part, safe="") for part in normalized_key.split("/"))
    return f"{base_url}/{encoded_key}"


def _build_design_preview_url(design_request_id: uuid.UUID) -> str:
    return f"{settings.api_v1_prefix}/designs/{design_request_id}/preview"


def _resolve_design_preview_url(
    current_user_id: uuid.UUID,
    design_request: DesignRequest,
) -> str | None:
    if design_request.input_r2_key and settings.r2_endpoint_url:
        return _build_design_preview_url(design_request.id)

    if design_request.input_filename:
        file_path = (
            Path(settings.design_upload_dir)
            / str(current_user_id)
            / design_request.input_filename
        )
        if file_path.exists():
            return _build_design_preview_url(design_request.id)

    return None


async def _find_existing_saved_item(
    db: AsyncSession,
    *,
    current_user_id: uuid.UUID,
    item_type: SavedItemType,
    discover_card_id: str | None = None,
    design_request_id: uuid.UUID | None = None,
) -> SavedItem | None:
    statement = select(SavedItem).where(
        SavedItem.user_id == current_user_id,
        SavedItem.item_type == item_type.value,
    )

    if item_type == SavedItemType.DISCOVER_CARD:
        statement = statement.where(SavedItem.discover_card_id == discover_card_id)
    else:
        statement = statement.where(SavedItem.design_request_id == design_request_id)

    result = await db.execute(statement.limit(1))
    return result.scalar_one_or_none()


async def _load_discover_card(
    db: AsyncSession,
    *,
    discover_card_id: str,
) -> tuple[DiscoverCard, DiscoverAsset] | None:
    result = await db.execute(
        select(DiscoverCard, DiscoverAsset)
        .join(
            DiscoverAsset,
            DiscoverAsset.asset_id == DiscoverCard.image_asset_id,
        )
        .where(DiscoverCard.card_id == discover_card_id)
        .limit(2)
    )
    rows = result.all()
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Discover card identifier is ambiguous in seed data. "
                "Please contact support."
            ),
        )
    card, asset = rows[0]
    return card, asset


async def _load_completed_design_request(
    db: AsyncSession,
    *,
    current_user_id: uuid.UUID,
    design_request_id: uuid.UUID,
) -> DesignRequest:
    result = await db.execute(
        select(DesignRequest).where(
            DesignRequest.id == design_request_id,
            DesignRequest.user_id == current_user_id,
            DesignRequest.deleted_at.is_(None),
        )
    )
    design_request = result.scalar_one_or_none()
    if design_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Design request not found",
        )

    if design_request.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only completed design requests can be saved",
        )

    return design_request


def _build_discover_saved_item_response(
    saved_item: SavedItem,
    *,
    discover_card: DiscoverCard,
    discover_asset: DiscoverAsset,
) -> SavedItemResponse:
    return SavedItemResponse(
        saved_item_id=saved_item.id,
        item_type=SavedItemType.DISCOVER_CARD,
        saved_at=saved_item.saved_at,
        discover_card_id=discover_card.card_id,
        category_key=discover_card.category_key,
        section_id=discover_card.section_id,
        section_title=discover_card.section_title,
        image_url=_build_public_r2_url(discover_asset.r2_key),
    )


def _build_design_saved_item_response(
    saved_item: SavedItem,
    *,
    current_user_id: uuid.UUID,
    design_request: DesignRequest,
) -> SavedItemResponse:
    return SavedItemResponse(
        saved_item_id=saved_item.id,
        item_type=SavedItemType.DESIGN_REQUEST,
        saved_at=saved_item.saved_at,
        design_request_id=design_request.id,
        building_type=design_request.building_type,
        style_id=design_request.style_id,
        palette_id=design_request.palette_id,
        output_preview_url=design_request.output_preview_url,
        preview_url=_resolve_design_preview_url(current_user_id, design_request),
    )


async def _create_or_restore_saved_item(
    db: AsyncSession,
    *,
    current_user_id: uuid.UUID,
    item_type: SavedItemType,
    discover_card_id: str | None = None,
    design_request_id: uuid.UUID | None = None,
) -> SavedItem:
    existing = await _find_existing_saved_item(
        db,
        current_user_id=current_user_id,
        item_type=item_type,
        discover_card_id=discover_card_id,
        design_request_id=design_request_id,
    )
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.saved_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(existing)
        return existing

    saved_item = SavedItem(
        user_id=current_user_id,
        item_type=item_type.value,
        discover_card_id=discover_card_id,
        design_request_id=design_request_id,
    )
    db.add(saved_item)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _find_existing_saved_item(
            db,
            current_user_id=current_user_id,
            item_type=item_type,
            discover_card_id=discover_card_id,
            design_request_id=design_request_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not save item due to concurrent update",
            ) from None
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.saved_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(existing)
        return existing

    await db.refresh(saved_item)
    return saved_item


@router.post(
    "/items",
    response_model=SavedItemResponse,
    summary="Save a discover card or completed design request",
)
async def save_item(
    payload: SavedItemMutationRequest,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> SavedItemResponse:
    if payload.item_type == SavedItemType.DISCOVER_CARD.value:
        row = await _load_discover_card(db, discover_card_id=payload.discover_card_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Discover card not found",
            )
        discover_card, discover_asset = row

        saved_item = await _create_or_restore_saved_item(
            db,
            current_user_id=current_user_id,
            item_type=SavedItemType.DISCOVER_CARD,
            discover_card_id=discover_card.card_id,
        )
        return _build_discover_saved_item_response(
            saved_item,
            discover_card=discover_card,
            discover_asset=discover_asset,
        )

    design_request = await _load_completed_design_request(
        db,
        current_user_id=current_user_id,
        design_request_id=payload.design_request_id,
    )
    saved_item = await _create_or_restore_saved_item(
        db,
        current_user_id=current_user_id,
        item_type=SavedItemType.DESIGN_REQUEST,
        design_request_id=design_request.id,
    )
    return _build_design_saved_item_response(
        saved_item,
        current_user_id=current_user_id,
        design_request=design_request,
    )


@router.delete(
    "/items",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsave a discover card or completed design request",
)
async def unsave_item(
    payload: SavedItemMutationRequest,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    item_type = SavedItemType(payload.item_type)
    discover_card_id = (
        payload.discover_card_id
        if item_type == SavedItemType.DISCOVER_CARD
        else None
    )
    design_request_id = (
        payload.design_request_id
        if item_type == SavedItemType.DESIGN_REQUEST
        else None
    )

    existing = await _find_existing_saved_item(
        db,
        current_user_id=current_user_id,
        item_type=item_type,
        discover_card_id=discover_card_id,
        design_request_id=design_request_id,
    )
    if existing is None or existing.deleted_at is not None:
        return

    existing.deleted_at = datetime.now(UTC)
    await db.commit()


@router.get(
    "/items",
    response_model=SavedItemsListResponse,
    summary="List the current user's saved items",
)
async def list_saved_items(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> SavedItemsListResponse:
    result = await db.execute(
        select(SavedItem)
        .where(
            SavedItem.user_id == current_user_id,
            SavedItem.deleted_at.is_(None),
        )
        .order_by(SavedItem.saved_at.desc())
        .limit(200)
    )
    saved_items = result.scalars().all()

    items: list[SavedItemResponse] = []
    for saved_item in saved_items:
        item_type = SavedItemType(saved_item.item_type)
        if item_type == SavedItemType.DISCOVER_CARD:
            if not saved_item.discover_card_id:
                continue

            row = await _load_discover_card(db, discover_card_id=saved_item.discover_card_id)
            if row is None:
                continue

            discover_card, discover_asset = row
            items.append(
                _build_discover_saved_item_response(
                    saved_item,
                    discover_card=discover_card,
                    discover_asset=discover_asset,
                )
            )
            continue

        if not saved_item.design_request_id:
            continue

        result = await db.execute(
            select(DesignRequest).where(
                DesignRequest.id == saved_item.design_request_id,
                DesignRequest.user_id == current_user_id,
                DesignRequest.deleted_at.is_(None),
            )
        )
        design_request = result.scalar_one_or_none()
        if design_request is None:
            continue
        if design_request.status != "completed":
            continue

        items.append(
            _build_design_saved_item_response(
                saved_item,
                current_user_id=current_user_id,
                design_request=design_request,
            )
        )

    return SavedItemsListResponse(items=items)
