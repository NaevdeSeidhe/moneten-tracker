"""Dritte Herkunftsstufe am Lohnposten: ``fortgeschrieben``.

Bisher kannte ``lohn_posten.herkunft`` zwei Stufen: ``erfasst`` (abgelesen) und
``gerechnet`` (aus einem Jahreswert oder Beitragssatz abgeleitet). Der haeufigste
Fall fiel zwischen beide — ein Monat OHNE eigenes Lohnblatt, dessen Zahlen aber
unveraendert aus einem belegten Monat stammen. Er lief als ``gerechnet`` mit und
untertrieb damit, was bekannt ist: der Betrag ist abgelesen, nur eben in einem
frueheren Monat. Stand die Marke ueberall, sagte sie nichts mehr.

Zwei Schritte, und beide sind noetig:

1. **Spalte verbreitern.** ``fortgeschrieben`` hat 15 Zeichen, die Spalte fasste
   10. SQLite erzwingt die Laenge zwar nicht, Postgres schon — und eine
   Migration, die nur unter SQLite haelt, ist keine.
2. **Bestand umschreiben.** Wer den Vorschlag eines frueheren Monats
   unveraendert gespeichert hat, hat einen fortgeschriebenen Posten in der DB
   stehen — als ``gerechnet``. Ohne diesen Schritt bliebe die neue Stufe leer,
   bis jeder Monat einmal neu gespeichert wird.

   Umgeschrieben wird nur, was sich BELEGEN laesst: ein gerechneter Posten,
   dessen Aufstellung eine Grundlage „uebernommen aus …" oder „fortgeschrieben
   aus …" traegt. Das ist genau der Text, den ``services.lohn.vorschlag`` beim
   Uebernehmen eines frueheren Monats setzt. Aus einem Jahreswert Abgeleitetes
   traegt „Jahreslohn … : 12" und bleibt unangetastet — Abschreiben macht aus
   einer Schaetzung keine Ablesung.

Revision ID: 0023_lohn_fortgeschrieben
Revises: 0022_fonds_start_jahresanfang
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_lohn_fortgeschrieben"
down_revision: str | None = "0022_fonds_start_jahresanfang"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("lohn_posten") as batch:
        batch.alter_column(
            "herkunft",
            existing_type=sa.String(length=10),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    # KEINE Bestands-Hebung mehr. Sie stand hier und war falsch: der Filter lief
    # auf ABRECHNUNGS-Ebene und hob danach jeden gerechneten Posten der
    # Aufstellung — auch die, die aus einem Beitragssatz stammen. Im Altbestand
    # gibt es kein Merkmal, das beide trennt; das Heben war ein Raten in
    # Richtung MEHR behaupteter Sicherheit. 0024 nimmt zurueck, was diese
    # Revision auf bereits ausgerollten Datenbanken angerichtet hat.
    #
    # Die Stufe entsteht ab jetzt nur noch dort, wo `vorschlag()` sie je Posten
    # vergibt — dort ist die Herkunft des einzelnen Postens bekannt.


def downgrade() -> None:
    # Die alte Spalte kennt die Stufe nicht — sie faellt auf „gerechnet"
    # zurueck. Das ist der Zustand vor dieser Revision und keine Verfaelschung:
    # fortgeschrieben ist auch dort nicht abgelesen.
    op.execute("UPDATE lohn_posten SET herkunft = 'gerechnet' WHERE herkunft = 'fortgeschrieben'")
    with op.batch_alter_table("lohn_posten") as batch:
        batch.alter_column(
            "herkunft",
            existing_type=sa.String(length=16),
            type_=sa.String(length=10),
            existing_nullable=False,
        )
