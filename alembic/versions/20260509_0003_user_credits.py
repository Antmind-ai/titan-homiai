"""Add user credit balance and credit ledger

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FREE_LIFETIME_CREDITS = 75


def upgrade() -> None:
    op.add_column(
        "platform_users",
        sa.Column("credit_balance", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "platform_credit_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("reference_id", sa.String(length=120), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("delta <> 0", name="ck_platform_credit_ledger_delta_nonzero"),
        sa.CheckConstraint(
            "balance_after >= 0",
            name="ck_platform_credit_ledger_balance_after_nonnegative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_platform_credit_ledger_user_id",
        "platform_credit_ledger",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_credit_ledger_user_id_created_at",
        "platform_credit_ledger",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ux_platform_credit_ledger_user_id_idempotency_key",
        "platform_credit_ledger",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.execute(
        sa.text(
            """
            UPDATE platform_users
            SET credit_balance = :free_credits
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
                :free_credits,
                :free_credits,
                'migration_bootstrap',
                'Initial free lifetime credits',
                'migration-0003-bootstrap',
                NULL
            FROM platform_users AS u
            """
        ).bindparams(free_credits=FREE_LIFETIME_CREDITS)
    )


def downgrade() -> None:
    op.drop_index(
        "ux_platform_credit_ledger_user_id_idempotency_key",
        table_name="platform_credit_ledger",
    )
    op.drop_index("ix_platform_credit_ledger_user_id_created_at", table_name="platform_credit_ledger")
    op.drop_index("ix_platform_credit_ledger_user_id", table_name="platform_credit_ledger")
    op.drop_table("platform_credit_ledger")

    op.drop_column("platform_users", "credit_balance")
