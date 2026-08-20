"""Manuelle Abos + ausgeblendete erkannte Händler.

Ermöglicht das Bearbeiten der Abos-Seite: manuell erfasste Abos (`manual_subscriptions`)
ergänzen die Auto-Erkennung; falsch erkannte werden über `dismissed_merchants`
(Händler-Schlüssel) ausgeblendet.

Revision ID: 0007_subscriptions_edit
Revises: 0006_drop_unused_schema
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_subscriptions_edit"
down_revision: str | None = "0006_drop_unused_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("interval", sa.String(10), nullable=False, server_default="monatlich"),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "dismissed_merchants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant_key", sa.String(120), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("dismissed_merchants")
    op.drop_table("manual_subscriptions")
