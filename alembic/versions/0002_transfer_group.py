"""Transfer-Verknüpfung: transfer_group_id zu transactions.

Verbindet die beiden Seiten eines Konto-Transfers (Umbuchung).

Revision ID: 0002_transfer_group
Revises: 0001_initial_schema
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_transfer_group"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("transfer_group_id", sa.String(36), nullable=True))
    op.create_index("ix_transactions_transfer_group", "transactions", ["transfer_group_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_transfer_group", table_name="transactions")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("transfer_group_id")
