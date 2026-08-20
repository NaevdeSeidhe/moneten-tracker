"""Merkung für nachgelieferte Vorgaben: Tabelle ``seed_marks``.

``ensure_extra_categories`` lief bei jedem Start und prüfte, ob eine Kategorie
dieses NAMENS existiert. Zwei Folgen, beide still:

* **Gelöscht kam zurück.** Wer eine der acht nachgelieferten Kategorien löschte,
  hatte sie nach dem nächsten Neustart wieder — in jeder Auswahl, ohne Meldung.
* **Umbenannt kam dazu.** Wer sie umbenannte, bekam sie ZUSÄTZLICH: die alten
  Buchungen hingen an der umbenannten, die neue war leer und in der Auswahl
  nicht zu unterscheiden. Ab da konnten Buchungen auf zwei Töpfe für dieselbe
  Sache laufen, und keine Auswertung stimmte mehr.

Ein Schlüssel AN der Kategorie hätte den ersten Fall nicht gelöst: mit der Zeile
ginge auch die Merkung. Deshalb eine eigene Tabelle. Sie sagt nicht „diese
Kategorie existiert", sondern „diese Vorgabe wurde dem Nutzer schon einmal
angeboten".

**Bestehende Installationen bekommen alle acht Schlüssel eingetragen.** In einer
Datenbank, in der schon Kategorien stehen, ist jede dieser Vorgaben mindestens
einmal angeboten worden — der Seed lief bei jedem Start. Ohne diesen Eintrag
käme eine bereits gelöschte Kategorie noch genau einmal zurück. Eine FRISCHE
Datenbank bleibt leer; dort legt der Seed die Kategorien an und merkt sie sich
dabei selbst.

Revision ID: 0031_seed_marken
Revises: 0030_bank_referenz
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0031_seed_marken"
down_revision: str | None = "0030_bank_referenz"
branch_labels: str | None = None
depends_on: str | None = None

_TABELLE = "seed_marks"

#: Muss zu ``_EXTRA_CATEGORIES`` in ``db/seeds.py`` passen. Bewusst hier
#: wiederholt und nicht importiert: eine Migration beschreibt einen Zustand von
#: damals; würde sie den heutigen Code lesen, änderte sich ihre Wirkung
#: rückwirkend, sobald jemand die Liste erweitert.
_SCHLUESSEL = [
    "technik", "snacks", "haushalt", "rueckzahlungen",
    "spenden", "weiterbildung", "bankgebuehren", "nicht_zuordenbar",
]


def _tabellen(bind: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if _TABELLE not in _tabellen(bind):
        op.create_table(
            _TABELLE,
            sa.Column("schluessel", sa.String(60), primary_key=True),
            sa.Column("angelegt_am", sa.DateTime(timezone=True), nullable=True),
        )

    # Ist das eine bestehende Anlage? Dann galten alle Vorgaben als angeboten.
    bestand = bind.execute(sa.text("SELECT COUNT(*) FROM categories")).scalar() or 0
    if not bestand:
        return

    schon_da = {
        r[0] for r in bind.execute(sa.text(f"SELECT schluessel FROM {_TABELLE}"))  # noqa: S608
    }
    for schluessel in _SCHLUESSEL:
        if schluessel in schon_da:
            continue
        bind.execute(
            sa.text(f"INSERT INTO {_TABELLE} (schluessel, angelegt_am) "  # noqa: S608
                    "VALUES (:s, CURRENT_TIMESTAMP)"),
            {"s": schluessel},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABELLE in _tabellen(bind):
        op.drop_table(_TABELLE)
