"""Treffen-Fonds: gemeinsamer Spar-Topf zweier Personen für Besuche.

* ``meet_fund_settings`` — eine Zeile: Monatsraten (CHF/EUR), manueller
  EUR→CHF-Kurs, Kosten-Faktoren (Flug/Airbnb/Verpflegung), Startmonat/-betrag.
* ``meet_contributions`` — bestätigte Monats-Rücklagen je Person.
* ``meet_visits`` — geplante/vergangene Treffen (Ort, Nächte).

Revision ID: 0015_meet_fund
Revises: 0014_transaction_indexes
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_meet_fund"
down_revision: str | None = "0014_transaction_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meet_fund_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("monthly_a_chf", sa.Numeric(14, 2), nullable=False, server_default="345"),
        sa.Column("monthly_b_eur", sa.Numeric(14, 2), nullable=False, server_default="100"),
        sa.Column("eur_chf_rate", sa.Numeric(8, 4), nullable=False, server_default="0.93"),
        sa.Column("flight_a_chf", sa.Numeric(14, 2), nullable=False, server_default="350"),
        sa.Column("flight_b_chf", sa.Numeric(14, 2), nullable=False, server_default="350"),
        sa.Column("airbnb_night_chf", sa.Numeric(14, 2), nullable=False, server_default="116"),
        sa.Column("food_day_chf", sa.Numeric(14, 2), nullable=False, server_default="30"),
        sa.Column("default_nights", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("start_month", sa.Date(), nullable=False, server_default="2026-07-01"),
        sa.Column("start_balance_chf", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.create_table(
        "meet_contributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("person", sa.String(10), nullable=False),
        sa.Column("amount_native", sa.Numeric(14, 2), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("month", "person", name="uq_meet_contribution"),
    )
    op.create_table(
        "meet_visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(10), nullable=False),
        sa.Column("nights", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("cost_override_chf", sa.Numeric(14, 2), nullable=True),
        sa.Column("notes", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("meet_visits")
    op.drop_table("meet_contributions")
    op.drop_table("meet_fund_settings")
