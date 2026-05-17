"""Add saved items table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-18 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saved_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(length=30), nullable=False),
        sa.Column("discover_card_id", sa.String(length=120), nullable=True),
        sa.Column("design_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "saved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "item_type IN ('discover_card', 'design_request')",
            name="ck_platform_saved_items_item_type",
        ),
        sa.CheckConstraint(
            "("
            "item_type = 'discover_card' AND discover_card_id IS NOT NULL AND design_request_id IS NULL"
            ") OR ("
            "item_type = 'design_request' AND design_request_id IS NOT NULL AND discover_card_id IS NULL"
            ")",
            name="ck_platform_saved_items_target_fields",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["platform_users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["design_request_id"],
            ["platform_design_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "discover_card_id",
            name="uq_platform_saved_items_user_discover_card",
        ),
        sa.UniqueConstraint(
            "user_id",
            "design_request_id",
            name="uq_platform_saved_items_user_design_request",
        ),
    )

    op.create_index(
        "ix_platform_saved_items_user_saved_at",
        "platform_saved_items",
        ["user_id", "saved_at"],
        unique=False,
    )
    op.create_index(
        "ix_platform_saved_items_discover_card_id",
        "platform_saved_items",
        ["discover_card_id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_saved_items_design_request_id",
        "platform_saved_items",
        ["design_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_saved_items_design_request_id",
        table_name="platform_saved_items",
    )
    op.drop_index(
        "ix_platform_saved_items_discover_card_id",
        table_name="platform_saved_items",
    )
    op.drop_index(
        "ix_platform_saved_items_user_saved_at",
        table_name="platform_saved_items",
    )
    op.drop_table("platform_saved_items")
