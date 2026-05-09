"""Add soft delete to design requests

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_design_requests",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_design_requests_user_id_not_deleted",
        "platform_design_requests",
        ["user_id", "deleted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_design_requests_user_id_not_deleted",
        table_name="platform_design_requests",
    )
    op.drop_column("platform_design_requests", "deleted_at")
