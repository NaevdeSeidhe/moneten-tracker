"""Manuelles Abo mit Bankbuchungen verbinden: Spalte ``match_keyword``.

Verknüpft ein manuell erfasstes Abo mit einem Händler-Schlüssel — alle Buchungen
dieses Händlers gelten als verbunden; der Händler wird dann nicht zusätzlich
automatisch als Abo erkannt (keine Doppelzählung).

Revision ID: 0011_subscription_match_keyword
Revises: 0010_transaction_splits
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_subscription_match_keyword"
down_revision: str | None = "0010_transaction_splits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manual_subscriptions",
        sa.Column("match_keyword", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("manual_subscriptions", "match_keyword")
