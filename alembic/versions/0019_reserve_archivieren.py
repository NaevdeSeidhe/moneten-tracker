"""„Unvorhergesehenes / Reserve" archiviert.

Die Kategorie stammt aus dem Startbestand (Modell der CH-Budgetberatung: eine
Rueckstellung fuer Unvorhergesehenes, die man monatlich einplant). Sie wurde nie
benutzt und steht seither in jeder Auswahl im Weg.

**Archiviert, nicht geloescht.** Haengt doch eine Buchung daran, ginge deren
Zuordnung beim Loeschen verloren — archiviert verschwindet die Kategorie aus
Auswahl und Budget, ohne etwas mitzunehmen. Nachgewiesen: eine Testbuchung auf
„Reserve" ueberlebt die Migration unveraendert.

Die Oberkategorie wandert nur mit, wenn darunter nichts Aktives mehr steht — wer
dort eigene Unterkategorien angelegt hat, behaelt sie samt Gruppe.

Die drei neuen Kategorien (Spenden, Weiterbildung / Studium, Bankgebuehren)
stehen bewusst NICHT hier, sondern in ``seeds._EXTRA_CATEGORIES``. Das ist der
dafuer vorgesehene Weg der Codebasis: idempotent, laeuft bei jedem Start und
deckt Neuinstallation wie Bestand ab. Eine Migration waere ein zweiter Mechanismus
fuer dieselbe Sache.

Revision ID: 0019_reserve_archiv
Revises: 0018_cash_goal
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_reserve_archiv"
down_revision: str | None = "0018_cash_goal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE categories SET is_archived = 1"
        " WHERE name = 'Reserve' AND parent_id IS NOT NULL"
    )
    op.execute(
        "UPDATE categories SET is_archived = 1"
        " WHERE name = 'Unvorhergesehenes' AND parent_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM categories k"
        "                 WHERE k.parent_id = categories.id AND k.is_archived = 0)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE categories SET is_archived = 0"
        " WHERE name IN ('Reserve', 'Unvorhergesehenes')"
    )
