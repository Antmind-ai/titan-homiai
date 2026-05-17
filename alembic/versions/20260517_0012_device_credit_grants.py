"""Track free credit grants per device

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-17 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FREE_LIFETIME_CREDITS = 25


def upgrade() -> None:
    op.create_table(
        "platform_device_credit_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("device_id", sa.String(length=255), nullable=False),
        sa.Column("first_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("credits_granted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["first_user_id"],
            ["platform_users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", name="uq_platform_device_credit_grants_device_id"),
    )

    op.create_index(
        "ix_platform_device_credit_grants_first_user_id",
        "platform_device_credit_grants",
        ["first_user_id"],
        unique=False,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO platform_device_credit_grants (
                id,
                device_id,
                first_user_id,
                credits_granted
            )
            SELECT
                uuid_generate_v4(),
                u.device_id,
                u.id,
                :free_credits
            FROM platform_users AS u
            ON CONFLICT (device_id) DO NOTHING
            """
        ).bindparams(free_credits=FREE_LIFETIME_CREDITS)
    )

    op.execute(
        sa.text(
            """
            INSERT INTO platform_credit_ledger (
                id,
                user_id,
                delta,
                balance_after,
                source,
                reason,
                reference_id,
                idempotency_key
            )
            SELECT
                uuid_generate_v4(),
                u.id,
                -u.credit_balance,
                0,
                'account_deletion_forfeit',
                'Existing deleted account credit forfeiture',
                'migration-0012-deleted-user-forfeit',
                NULL
            FROM platform_users AS u
            WHERE u.deleted_at IS NOT NULL
              AND u.credit_balance > 0
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE platform_users
            SET credit_balance = 0
            WHERE deleted_at IS NOT NULL
              AND credit_balance > 0
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_device_credit_grants_first_user_id",
        table_name="platform_device_credit_grants",
    )
    op.drop_table("platform_device_credit_grants")
