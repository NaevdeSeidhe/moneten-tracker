"""Bargeld-Ziel: gewünschter Bar-Anteil an den Alltagsausgaben.

Für die Auswertung „Bar gegen digital". 0 bedeutet „kein Ziel gesetzt" — dann
zeigt die App nur den Stand, ohne Ziellinie und ohne Bewertung.

Revision ID: 0018_cash_goal
Revises: 0017_reactor_weg
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_cash_goal"
down_revision: str | None = "0017_reactor_weg"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("cash_goal_pct", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "cash_goal_pct")
