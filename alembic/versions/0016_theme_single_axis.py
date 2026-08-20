"""Theme einachsig: preferred_theme als freier String, preferred_reactor entfällt.

Vorher zwei Achsen — ``preferred_theme`` (Enum dark|light) UND ``preferred_reactor``
(Boolean). Das liess sich nicht auf weitere Farbwelten erweitern: „Reactor" ist kein
Modus von Dark, sondern eine eigene Welt. Jetzt EIN Name pro Theme; neue Themes
brauchen nur einen Block in ``skins.css`` + Eintrag in ``src/moneten/themes.py`` —
keine weitere Migration.

Datenerhalt: ``preferred_reactor = 1`` wird zu ``preferred_theme = 'reactor'``.

Revision ID: 0016_theme_single_axis
Revises: 0015_meet_fund
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_theme_single_axis"
down_revision: str | None = "0015_meet_fund"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) Spalte verbreitern (Enum-Länge 8 reichte nur für "dark"/"light").
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "preferred_theme",
            existing_type=sa.String(length=8),
            type_=sa.String(length=20),
            existing_nullable=False,
            server_default="dark",
        )
    # 2) Bestehende Reactor-Nutzer übernehmen — VOR dem Löschen der Spalte.
    op.execute("UPDATE users SET preferred_theme = 'reactor' WHERE preferred_reactor = 1")
    # 3) Zweite Achse entfällt.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("preferred_reactor")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("preferred_reactor", sa.Boolean(), nullable=False, server_default="0")
        )
    op.execute("UPDATE users SET preferred_reactor = 1 WHERE preferred_theme = 'reactor'")
    # Themes ohne Gegenstück im alten Modell fallen auf Dark zurück.
    op.execute("UPDATE users SET preferred_theme = 'dark' WHERE preferred_theme NOT IN ('dark','light')")
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "preferred_theme",
            existing_type=sa.String(length=20),
            type_=sa.String(length=8),
            existing_nullable=False,
            server_default="dark",
        )
