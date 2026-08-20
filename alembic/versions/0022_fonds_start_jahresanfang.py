"""Treffen-Fonds: der Startmonat war ein fest eingetragenes Datum.

``start_month`` bestimmt, wie weit die Monatsliste des Fonds zurückreicht —
weiter zurück lässt sich kein vergessener Beitrag nachtragen. Die Vorgabe stand
zweimal fest verdrahtet im Code: als ``default`` im Modell und als
``server_default`` hier in 0015 (beide 1.7.2026). Ein festes Datum veraltet
lautlos: es bleibt stehen, während die Gegenwart weiterläuft, und mit jedem
Monat schrumpft die Vergangenheit, die die Oberfläche überhaupt anbietet.

Zwei Schritte, dieselbe Ursache:

* Der ``server_default`` fällt weg. Die Vorgabe gehört an EINEN Ort; das Modell
  leitet sie jetzt ab (1. Januar des laufenden Jahres beim ersten Zugriff).
  Ohne Default schlägt ein ``INSERT`` ohne Startmonat hörbar fehl, statt still
  ein Datum von 2026 einzutragen.
* Bestehende Zeilen wandern auf den 1. Januar IHRES Startjahres. Nur zurück:
  nach vorn fiele ein bereits erfasster Beitrag aus der Monatsliste — er zählte
  im Topf weiter mit, stünde aber nirgends mehr, und der Stand wäre nicht mehr
  herleitbar.

Revision ID: 0022_fonds_start_jahresanfang
Revises: 0021_lohnzusammensetzung
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa

from alembic import op

revision: str = "0022_fonds_start_jahresanfang"
down_revision: str | None = "0021_lohnzusammensetzung"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meet_fund_settings") as batch:
        batch.alter_column("start_month", existing_type=sa.Date(),
                           existing_nullable=False, server_default=None)

    conn = op.get_bind()
    for zid, roh in conn.execute(sa.text("SELECT id, start_month FROM meet_fund_settings")).all():
        # SQLite liefert Datumsspalten je nach Treiber als Text zurück.
        start = roh if isinstance(roh, date) else date.fromisoformat(str(roh)[:10])
        jahresanfang = start.replace(month=1, day=1)
        if start > jahresanfang:
            conn.execute(
                sa.text("UPDATE meet_fund_settings SET start_month = :s WHERE id = :i"),
                {"s": jahresanfang.isoformat(), "i": zid},
            )


def downgrade() -> None:
    # Nur das Schema geht zurück. Der frühere Startmonat ist nicht rekonstruierbar
    # — und ihn pauschal wieder vorzuschieben, würde erfasste Beiträge aus der
    # Liste drängen.
    with op.batch_alter_table("meet_fund_settings") as batch:
        batch.alter_column("start_month", existing_type=sa.Date(),
                           existing_nullable=False, server_default="2026-07-01")
