"""Reactor-Theme entfernt + Konto „Vorsorgekonto 3a" → „Säule 3a".

Zwei kleine Datenkorrekturen, beide idempotent und bewusst eng gefasst:

1. **Theme**: Der Reactor-Skin ist entfallen (eigene CSS-Datei, eigene Geometrie —
   optisch nicht überzeugend). Wer ihn eingestellt hatte, bekäme sonst einen
   Namen, den es nicht mehr gibt. ``themes.get()`` fiele zwar sicher auf Dunkel
   zurück, in der DB stünde aber weiter ein toter Wert.

2. **Kontoname**: „Vorsorgekonto 3a" ist in der Konten-Liste zu breit und drängt
   den Saldo aus der Zeile. „Säule 3a" ist der geläufige Schweizer Begriff.
   Umbenannt wird NUR bei exakt diesem Namen — wer sein Konto selbst anders
   benannt hat, behält seinen Namen.

Revision ID: 0017_reactor_weg
Revises: 0016_theme_single_axis
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_reactor_weg"
down_revision: str | None = "0016_theme_single_axis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET preferred_theme = 'dark' WHERE preferred_theme = 'reactor'")
    op.execute("UPDATE accounts SET name = 'Säule 3a' WHERE name = 'Vorsorgekonto 3a'")


def downgrade() -> None:
    # Der Kontoname lässt sich zurückdrehen; das Theme nicht — „reactor" gibt es
    # nicht mehr, ein Zurücksetzen würde nur einen toten Wert wiederherstellen.
    op.execute("UPDATE accounts SET name = 'Vorsorgekonto 3a' WHERE name = 'Säule 3a'")
