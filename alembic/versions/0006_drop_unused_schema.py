"""Ungenutztes Schema entfernen (Tabellen + Spalten, die nie befüllt wurden).

Entfernt Altlasten aus dem ursprünglichen Entwurf, die im Code nirgends mehr
gelesen/geschrieben werden:

* Tabellen ``tags`` + ``transaction_tags`` (Tag-Feature nie gebaut),
  ``recurring_templates`` (Vorlagen — durch die automatische Abo-Erkennung
  ersetzt) und ``categorization_rules`` (durch ``category_rules`` + Lern-Engine
  ersetzt).
* Spalten der ``transactions``-Tabelle: ``attachment_path`` (Anhänge laufen über
  die Tabelle ``attachments``), ``recurring_template_id`` (FK auf die gedroppte
  Vorlagen-Tabelle), ``parent_transaction_id`` + ``is_split`` (Split-Buchungen
  nie umgesetzt).

SQLite kann Spalten nur über einen Tabellen-Neubau löschen → ``batch_alter_table``.
Die echten Buchungsdaten werden dabei 1:1 in die neue Tabelle kopiert.

Revision ID: 0006_drop_unused_schema
Revises: 0005_category_rules
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_drop_unused_schema"
down_revision: str | None = "0005_category_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) Tote Spalten aus transactions entfernen (Tabellen-Neubau via Batch-Mode).
    #    Reihenfolge zuerst, weil recurring_template_id eine FK auf
    #    recurring_templates hält — die Spalte muss weg, bevor die Tabelle fällt.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("attachment_path")
        batch_op.drop_column("recurring_template_id")
        batch_op.drop_column("parent_transaction_id")
        batch_op.drop_column("is_split")

    # 2) Ungenutzte Tabellen entfernen. transaction_tags zuerst (FK auf tags).
    op.drop_table("transaction_tags")
    op.drop_table("tags")
    op.drop_table("recurring_templates")
    op.drop_table("categorization_rules")


def downgrade() -> None:
    # Best-effort-Rückbau: Tabellen + Spalten wieder anlegen (Daten sind verloren,
    # waren aber ohnehin leer/ungenutzt).
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("color", sa.String(9), nullable=True),
    )
    op.create_table(
        "transaction_tags",
        sa.Column("transaction_id", sa.Integer(), primary_key=True),
        sa.Column("tag_id", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "recurring_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("frequency", sa.String(12), nullable=False, server_default="monthly"),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("next_due_date", sa.Date(), nullable=True),
        sa.Column("auto_post", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_subscription", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("conditions_json", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
    )
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("attachment_path", sa.String(500), nullable=True))
        batch_op.add_column(sa.Column("recurring_template_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("parent_transaction_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("is_split", sa.Boolean(), nullable=False, server_default=sa.false()))
