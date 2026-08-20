"""Verlaufsreihen: Praemie, Strom, Lohn, Vorsorge, Steuern.

Zwei neue Tabellen fuer Werte, die aus **Belegen** stammen statt aus dem Konto:
Praemienabrechnung, Stromrechnung, Vorsorgeausweis, Steuerveranlagung, Police.

**Warum keine Buchungen.** Die Krankenkassenpraemie steht als monatliche
Belastung laengst in ``transactions``. Wuerde der Beleg zusaetzlich als Buchung
importiert, zaehlte jeder Monat doppelt — die Monatsbilanz waere um den
Praemienbetrag falsch. Die Reihen sind darum eine eigene Schicht daneben, die
man gegen die Buchungen vergleichen kann, statt sie zu vermischen.

``metric_points.extras`` ist JSON, weil die Zahl der Nebenwerte je Reihe
verschieden ist: Strom fuehrt kWh und Zaehlerstaende, die Vorsorge fuehrt
versicherten Lohn und Beitraege von Arbeitnehmer und Arbeitgeber. Feste Spalten
fuer jede dieser Groessen waeren zu drei Vierteln leer.

``metric_series.category_id`` verknuepft eine Reihe mit der Kategorie, in der ihre
Zahlungen gebucht sein sollten. Darauf beruht der Soll/Ist-Abgleich: der Beleg sagt,
was verlangt wurde, die Buchungen sagen, was wirklich abging. Bewusst eine Spalte
statt einer Stichwortsuche ueber den Kategorienamen — der Steuerauszug macht es so,
und dort bleiben Positionen stillschweigend leer, wenn kein Name passt.

Die Reihen-Definitionen selbst stehen NICHT hier, sondern in
``seeds._METRIC_SERIES`` — derselbe idempotente Weg wie bei den Kategorien
(0019). Diese Migration legt nur die Tabellen an.

Revision ID: 0020_verlaeufe
Revises: 0019_reserve_archiv
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_verlaeufe"
down_revision: str | None = "0019_reserve_archiv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metric_series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "unit",
            sa.Enum("chf", "kwh", "prozent", name="metricunit", native_enum=False, length=10),
            nullable=False,
        ),
        sa.Column(
            "cadence",
            sa.Enum(
                "monatlich",
                "quartalsweise",
                "jaehrlich",
                "unregelmaessig",
                name="metriccadence",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "ausgabe",
                "einnahme",
                "vermoegen",
                name="metrickind",
                native_enum=False,
                length=12,
            ),
            nullable=False,
        ),
        sa.Column("secondary_key", sa.String(length=30), nullable=True),
        sa.Column(
            "secondary_unit",
            sa.Enum("chf", "kwh", "prozent", name="metricunit", native_enum=False, length=10),
            nullable=True,
        ),
        sa.Column("secondary_label", sa.String(length=40), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metric_series_slug", "metric_series", ["slug"], unique=True)

    op.create_table(
        "metric_points",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["metric_series.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "period_start", name="uq_metric_point_periode"),
    )
    op.create_index("ix_metric_points_series_id", "metric_points", ["series_id"])


def downgrade() -> None:
    # Reihenfolge zaehlt: erst die Kind-Tabelle, sonst haengt der Fremdschluessel.
    op.drop_index("ix_metric_points_series_id", table_name="metric_points")
    op.drop_table("metric_points")
    op.drop_index("ix_metric_series_slug", table_name="metric_series")
    op.drop_table("metric_series")
