"""Foto-Belege: vorgemerkte Quittungen + gelernte Positions-Kategorien.

* ``pending_receipts`` — digitalisierte Belege (v.a. Handy-Foto), die noch keiner
  Buchung zugeordnet sind; werden automatisch zugeordnet, sobald Betrag + Datum
  einer Bankbuchung passen.
* ``receipt_item_rules`` — gelernte Position→Kategorie-Zuordnungen.
* ``users.receipt_photo_keep`` — Foto nach OCR verwerfen (Default) oder reduziert behalten.

Revision ID: 0013_receipt_photo_upload
Revises: 0012_user_reactor_theme
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_receipt_photo_upload"
down_revision: str | None = "0012_user_reactor_theme"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("receipt_photo_keep", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "pending_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant", sa.String(160), nullable=True),
        sa.Column("receipt_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("items_json", sa.Text(), nullable=True),
        sa.Column("source", sa.String(10), nullable=False, server_default="photo"),
        sa.Column("image_path", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "receipt_item_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keyword", sa.String(120), nullable=False),
        sa.Column("merchant_key", sa.String(120), nullable=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("keyword", "merchant_key", name="uq_receipt_item_rule"),
    )


def downgrade() -> None:
    op.drop_table("receipt_item_rules")
    op.drop_table("pending_receipts")
    op.drop_column("users", "receipt_photo_keep")
