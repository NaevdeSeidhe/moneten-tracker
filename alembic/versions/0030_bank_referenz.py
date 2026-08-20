"""Bank-Referenz an der Buchung: ``transactions.bank_reference``.

Die Duplikat-Erkennung lief über ``Datum | Betrag | erste 50 Zeichen Text``.
Zwei Fälle gingen dabei verloren, und zwar beide in Richtung „zu wenig Buchungen":

1. Zwei verschiedene Buchungen, deren Text sich erst ab Zeichen 51 unterscheidet
   (Dauerauftrag mit langer Referenz, Zahlungen an denselben Empfänger mit
   unterschiedlichem Zweck) — die zweite wurde als Dublette übersprungen.
2. Zwei **wirklich gleiche** Buchungen am selben Tag: zweimal derselbe Betrag im
   selben Laden. Inhaltlich nicht zu unterscheiden, für die Kasse aber zwei
   Vorgänge. Ein Inhalts-Hash kann diesen Fall grundsätzlich nicht lösen.

Der CAMT.053-Auszug bringt beides mit: ``AcctSvcrRef``, die Referenz der Bank.
Der Leser hat sie gelesen und weggeworfen. Ab hier wird sie gespeichert und ist
der Schlüssel, wenn sie da ist.

**Bestehende Buchungen bleiben unangetastet.** Sie haben keine Referenz — die
Spalte bleibt bei ihnen NULL, und für sie entscheidet weiterhin der Inhalts-Hash.
Ein erneuter Import eines schon importierten Auszugs erzeugt also keine
Dubletten: der Import prüft beide Schlüssel (siehe ``routers/import_bank.py``).
Nachträglich füllen liesse sich die Spalte nicht, weil die Referenz in keiner
gespeicherten Buchung mehr steckt.

Revision ID: 0030_bank_referenz
Revises: 0029_pin_wechsel_erzwingen
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0030_bank_referenz"
down_revision: str | None = "0029_pin_wechsel_erzwingen"
branch_labels: str | None = None
depends_on: str | None = None

_TABELLE = "transactions"
_SPALTE = "bank_reference"
_INDEX = "ix_transactions_bank_reference"


def _spalten(bind: sa.engine.Connection) -> set[str]:
    return {r[1] for r in bind.execute(sa.text(f'PRAGMA table_info("{_TABELLE}")'))}


def upgrade() -> None:
    """Fügt die Spalte samt Index hinzu — idempotent.

    ``ADD COLUMN`` braucht in SQLite kein ``batch_alter_table`` und damit auch
    keine Hilfstabelle: es gibt hier nichts, was bei einem Abbruch als
    ``_alembic_tmp_transactions`` liegenbleiben könnte. Die Abfrage vorher ist
    trotzdem da, damit ein zweiter Lauf nicht an „duplicate column" scheitert.
    """
    bind = op.get_bind()
    if _SPALTE not in _spalten(bind):
        op.add_column(_TABELLE, sa.Column(_SPALTE, sa.String(64), nullable=True))
    # Der Index trägt die Duplikat-Prüfung: sie fragt bei JEDER importierten Zeile
    # nach der Referenz. Ohne Index wäre das ein Full-Table-Scan pro Buchung.
    vorhandene = {i["name"] for i in sa.inspect(bind).get_indexes(_TABELLE)}
    if _INDEX not in vorhandene:
        op.create_index(_INDEX, _TABELLE, [_SPALTE])


def downgrade() -> None:
    bind = op.get_bind()
    vorhandene = {i["name"] for i in sa.inspect(bind).get_indexes(_TABELLE)}
    if _INDEX in vorhandene:
        op.drop_index(_INDEX, table_name=_TABELLE)
    if _SPALTE in _spalten(bind):
        op.drop_column(_TABELLE, _SPALTE)
