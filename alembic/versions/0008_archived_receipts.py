"""Archivierte Quittungen (ohne Bankeintrag abgelegt).

Belege ohne passende Bank-Buchung (z.B. vor dem Start der E-Banking-Daten)
werden hier abgelegt und verschwinden aus dem Zuordnungs-Assistenten.

Revision ID: 0008_archived_receipts
Revises: 0007_subscriptions_edit
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_archived_receipts"
down_revision: str | None = "0007_subscriptions_edit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "archived_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False, unique=True),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("archived_receipts")
