"""Artikel-Alias: die App merkt sich eine korrigierte Schreibweise.

Die Erkennung liest denselben Artikel auf jedem Beleg etwas anders. Wer das im
Editor richtigstellt, hatte es beim naechsten Beleg wieder zu tun — und der
Preisverlauf fuehrte eine Ware unter drei Namen.

Revision ID: 0027_artikel_alias
Revises: 0026_scan_protokoll
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_artikel_alias"
down_revision: str | None = "0026_scan_protokoll"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artikel_alias",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias_key", sa.String(length=200), nullable=False),
        sa.Column("kanonisch", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artikel_alias_alias_key", "artikel_alias", ["alias_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_artikel_alias_alias_key", table_name="artikel_alias")
    op.drop_table("artikel_alias")
