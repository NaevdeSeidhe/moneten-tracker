"""standard_budgets: Standard-Soll je Kategorie + Intervall (monatlich/jährlich).

Ermöglicht das einmalige Ausfüllen eines Soll-Betrags, der fortlaufend gilt.
Bei ``interval='jaehrlich'`` ist der Betrag der Jahreswert (1/12 ins
Monatsbudget, zusätzlich als Rückstellung gelistet).

Revision ID: 0004_standard_budget
Revises: 0003_attachment_filepath_nullable
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_standard_budget"
down_revision: str | None = "0003_attachment_filepath_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "standard_budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("interval", sa.String(10), nullable=False, server_default="monatlich"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("category_id", name="uq_standard_budget_cat"),
    )


def downgrade() -> None:
    op.drop_table("standard_budgets")
