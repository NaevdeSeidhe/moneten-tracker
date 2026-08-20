"""Lohnzusammensetzung: was hinter einer Lohn-Gutschrift steckt.

Zwei Tabellen an der Buchung: ``lohn_abrechnungen`` (eine je Gutschrift) und
``lohn_posten`` (Bruttolohn, Zulagen, Abzuege).

**Warum ``herkunft`` an jedem Posten steht.** Es gibt keine monatlichen
Lohnabrechnungen — ableitbar ist ein Monat nur aus Jahreswerten (Lohnausweis,
Vorsorgeausweis) oder aus einem gesetzlichen Beitragssatz. Ein Monat mit Bonus,
Nachzahlung oder Pensumsaenderung stimmt dann nicht, und man sieht es der Zahl
nicht an. Die Spalte haelt fest, welche Betraege abgelesen und welche
hergeleitet sind; die Anzeige kennzeichnet die hergeleiteten.

**Warum es keine Netto-Spalte gibt.** Der Nettolohn ergibt sich aus den Posten
und wird dem gebuchten Betrag gegenuebergestellt. Gespeichert liesse er sich an
die Buchung angleichen — dann saehe eine geschaetzte Aufstellung exakt aus.

Revision ID: 0021_lohnzusammensetzung
Revises: 0020_verlaeufe
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_lohnzusammensetzung"
down_revision: str | None = "0020_verlaeufe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lohn_abrechnungen",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("grundlage", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Eindeutig: genau eine Aufstellung je Buchung. Zwei waeren zwei Antworten
    # auf dieselbe Frage, und die Anzeige muesste raten, welche gilt.
    op.create_index(
        "ix_lohn_abrechnungen_transaction_id",
        "lohn_abrechnungen",
        ["transaction_id"],
        unique=True,
    )

    op.create_table(
        "lohn_posten",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("abrechnung_id", sa.Integer(), nullable=False),
        sa.Column(
            "art",
            sa.Enum("brutto", "abzug", name="lohnpostenart", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("betrag", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "herkunft",
            sa.Enum(
                "erfasst", "gerechnet", name="lohnherkunft", native_enum=False, length=10
            ),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["abrechnung_id"], ["lohn_abrechnungen.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lohn_posten_abrechnung_id", "lohn_posten", ["abrechnung_id"])


def downgrade() -> None:
    # Reihenfolge zaehlt: erst die Kind-Tabelle, sonst haengt der Fremdschluessel.
    op.drop_index("ix_lohn_posten_abrechnung_id", table_name="lohn_posten")
    op.drop_table("lohn_posten")
    op.drop_index("ix_lohn_abrechnungen_transaction_id", table_name="lohn_abrechnungen")
    op.drop_table("lohn_abrechnungen")
