"""Reactor-Skin (Dionysos) als User-Vorliebe: Spalte ``preferred_reactor``.

Orthogonal zu ``preferred_theme`` (dark/light): an/aus überlagert das ganze
Erscheinungsbild mit dem Reactor-Theme; Clean behält Dunkel/Hell.

Revision ID: 0012_user_reactor_theme
Revises: 0011_subscription_match_keyword
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_user_reactor_theme"
down_revision: str | None = "0011_subscription_match_keyword"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferred_reactor", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("users", "preferred_reactor")
