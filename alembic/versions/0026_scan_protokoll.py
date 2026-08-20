"""Scan-Protokoll: der Rohtext jeder Beleg-Erkennung bleibt stehen.

Bisher war er nur im offenen Dialog zu haben. Fenster zu, Text weg — und wer
einen Erkennungsfehler melden wollte, musste den Beleg abfotografieren. Das
Protokoll haelt die letzten Scans fest, damit ein Fehler nachstellbar bleibt.

Nur Text, kein Bild: das Foto zu behalten ist eine eigene Entscheidung
(``User.receipt_photo_keep``, standardmaessig aus).

Revision ID: 0026_scan_protokoll
Revises: 0025_ferienkonto_rueckstellung
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_scan_protokoll"
down_revision: str | None = "0025_ferienkonto_rueckstellung"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_protokoll",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("haendler", sa.String(length=160), nullable=True),
        sa.Column("betrag", sa.Numeric(14, 2), nullable=True),
        sa.Column("beleg_datum", sa.Date(), nullable=True),
        sa.Column("methode", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("positionen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ocr_text", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_scan_protokoll_created_at", "scan_protokoll", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_scan_protokoll_created_at", table_name="scan_protokoll")
    op.drop_table("scan_protokoll")
