"""Nimmt die Bestands-Hebung aus 0023 zurueck.

0023 hat beim Heben auf ABRECHNUNGS-Ebene gefiltert und danach JEDEN
gerechneten Posten dieser Aufstellung auf ``fortgeschrieben`` gesetzt.
Nachgemessen an einer Aufstellung mit drei gerechneten Posten (Bruttolohn aus
einem frueheren Blatt, AHV aus dem gesetzlichen Satz, Pensionskasse geschaetzt):
alle drei wurden gehoben, zwei davon zu Unrecht.

Der Fehler ist nicht der Filter, sondern die Annahme dahinter. Im ALTBESTAND
gibt es kein Merkmal, das die Kopie eines abgelesenen Postens von einem aus
einem Satz gerechneten trennt — die alte Uebernahme schrieb beides als
``gerechnet`` weg. Was 0023 tat, war deshalb kein Belegen, sondern ein Raten,
und es riet in die gefaehrliche Richtung: es behauptete MEHR Sicherheit, als
die Daten hergeben. Genau das soll dieses Modul nirgends tun (siehe
``services/lohn.py``, Modul-Docstring).

Dazu kam eine zweite Falle: die Grundlage ist freier Text. „uebernommen aus dem
Vertrag" traf das Muster ebenso wie „uebernommen aus Maerz 2026" — ein Satz,
den man beim Erfassen naheliegend tippt, hob damit eine ganze Aufstellung.

Diese Revision setzt den Bestand zurueck auf ``gerechnet``. Das ist die
ehrliche Stufe: fuer diese Posten IST nicht bekannt, ob sie auf ein Blatt
zurueckgehen. ``fortgeschrieben`` entsteht ab jetzt nur noch dort, wo
``vorschlag()`` es je Posten selbst vergibt — dort ist die Herkunft des
einzelnen Postens bekannt.

Was dabei verlorengeht: nichts, das belegt waere. Wer einen Monat erneut
speichert, bekommt die richtige Stufe je Posten.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_lohn_hebung_zuruecknehmen"
down_revision: str | None = "0023_lohn_fortgeschrieben"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALLE fortgeschriebenen Posten, nicht nur die von 0023 gehobenen: welche
    # aus der Hebung stammen und welche aus einem seither gespeicherten Monat,
    # steht nirgends. Ein Monat, den der Nutzer inzwischen erfasst hat, bekommt
    # seine Stufe beim naechsten Speichern zurueck — eine falsche Stufe
    # stillschweigend stehenzulassen waere teurer.
    op.execute(
        sa.text("UPDATE lohn_posten SET herkunft = 'gerechnet' "
                "WHERE herkunft = 'fortgeschrieben'")
    )


def downgrade() -> None:
    # Bewusst leer: die Hebung war falsch, sie wird nicht wiederhergestellt.
    # Ein downgrade, das einen bekannten Fehler zurueckholt, waere keiner.
    pass
