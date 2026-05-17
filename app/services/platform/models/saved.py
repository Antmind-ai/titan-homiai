from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SavedItem(Base):
    __tablename__ = "platform_saved_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('discover_card', 'design_request')",
            name="ck_platform_saved_items_item_type",
        ),
        CheckConstraint(
            "("
            "item_type = 'discover_card' "
            "AND discover_card_id IS NOT NULL "
            "AND design_request_id IS NULL"
            ") OR ("
            "item_type = 'design_request' "
            "AND design_request_id IS NOT NULL "
            "AND discover_card_id IS NULL"
            ")",
            name="ck_platform_saved_items_target_fields",
        ),
        UniqueConstraint(
            "user_id",
            "discover_card_id",
            name="uq_platform_saved_items_user_discover_card",
        ),
        UniqueConstraint(
            "user_id",
            "design_request_id",
            name="uq_platform_saved_items_user_design_request",
        ),
        Index(
            "ix_platform_saved_items_user_saved_at",
            "user_id",
            "saved_at",
        ),
        Index(
            "ix_platform_saved_items_discover_card_id",
            "discover_card_id",
        ),
        Index(
            "ix_platform_saved_items_design_request_id",
            "design_request_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_type: Mapped[str] = mapped_column(String(30), nullable=False)
    discover_card_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    design_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_design_requests.id", ondelete="CASCADE"),
        nullable=True,
    )
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
