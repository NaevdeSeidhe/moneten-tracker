"""Auto-Split: eine Buchung in mehrere Kategorie-Anteile aufteilen.

Neue Tabelle ``transaction_splits`` (Kategorie-Anteile je Buchung) plus die
Spalte ``transactions.is_split`` als schnelles Flag. Die Summe der Splits
entspricht dem Buchungsbetrag; der Saldo bleibt unberührt.

Revision ID: 0010_transaction_splits
Revises: 0009_subscription_kind
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_transaction_splits"
down_revision: str | None = "0009_subscription_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("is_split", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "transaction_splits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_transaction_splits_transaction_id", "transaction_splits", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_transaction_splits_transaction_id", table_name="transaction_splits")
    op.drop_table("transaction_splits")
    op.drop_column("transactions", "is_split")
