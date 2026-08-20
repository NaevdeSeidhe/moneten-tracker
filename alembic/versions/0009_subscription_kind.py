"""Art-Feld für manuelle Abos: Abo vs. wiederkehrende Zahlung.

Trennt auf der Abos-Seite klar zwischen echten Abos (Handy/Software/Games/
Streaming) und wiederkehrenden Zahlungen (Miete/Strom/GA/Steuern).

Revision ID: 0009_subscription_kind
Revises: 0008_archived_receipts
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_subscription_kind"
down_revision: str | None = "0008_archived_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manual_subscriptions",
        sa.Column("kind", sa.String(10), nullable=False, server_default="abo"),
    )


def downgrade() -> None:
    op.drop_column("manual_subscriptions", "kind")
