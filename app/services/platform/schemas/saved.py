from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, Field


class SavedItemType(StrEnum):
    DISCOVER_CARD = "discover_card"
    DESIGN_REQUEST = "design_request"


class SaveDiscoverCardItemRequest(BaseModel):
    item_type: Literal["discover_card"]
    discover_card_id: str = Field(..., min_length=1, max_length=120)


class SaveDesignRequestItemRequest(BaseModel):
    item_type: Literal["design_request"]
    design_request_id: uuid.UUID


SavedItemMutationRequest = Annotated[
    SaveDiscoverCardItemRequest | SaveDesignRequestItemRequest,
    Field(discriminator="item_type"),
]


class SavedItemResponse(BaseModel):
    saved_item_id: uuid.UUID
    item_type: SavedItemType
    saved_at: datetime

    discover_card_id: str | None = None
    category_key: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    image_url: str | None = None

    design_request_id: uuid.UUID | None = None
    building_type: str | None = None
    style_id: str | None = None
    palette_id: str | None = None
    output_preview_url: str | None = None
    preview_url: str | None = None


class SavedItemsListResponse(BaseModel):
    items: list[SavedItemResponse]
