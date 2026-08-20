"""Textebene eines PDFs als **Spaltentext**: eine Zeile je Tabellenzeile.

Die gewöhnliche Lesereihenfolge (``page.get_text()``) reicht für Fliesstext, aber
nicht für eine Positionstabelle: dort ist nicht mehr zu erkennen, welche Zahl
einer Zeile der Betrag ist. Menge, Preis pro Einheit und Betrag sind je nach
Position unterschiedlich besetzt — mal stehen zwei Zahlen da, mal drei, und die
zweite bedeutet einmal den Preis und einmal den Betrag. Über die Wort-Koordinaten
bleibt die Spaltenordnung erhalten, und die letzte Zelle ist immer der Betrag.

Diese Extraktion steht **im Paket und nicht im Skript**, weil sie zwei Aufrufer
hat: das lokale Extraktionsskript (``scripts/verlaeufe_aus_scans.py``, das daraus
die Verlaufsreihe speist) und die App selbst (die dieselbe Rechnung als Beleg an
die Buchung hängt). Zwei Kopien derselben Koordinatenlogik würden auseinander
driften, und die Schwelle, ab der eine neue Spalte beginnt, ist genau die Art
Zahl, die nur an EINER Stelle stehen darf.

Gedeutet wird hier nichts — das tut ``belege_parser``.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

# Zwei Wörter gehören zur selben Tabellenzeile, wenn ihre Grundlinien höchstens
# so weit auseinanderliegen. Gemessen an den Anbieter-Rechnungen: innerhalb einer
# Zeile schwankt die Grundlinie um Bruchteile eines Punktes, die nächste Zeile
# liegt gut zehn Punkte tiefer.
_ZEILE_PT = 3.0
# Ab dieser Lücke beginnt eine neue Spalte. Gemessen über alle Rechnungen:
# Wortabstände innerhalb einer Zelle sind 1 bis 2 pt, der kleinste Abstand
# zwischen zwei Spalten 16 pt. Dazwischen liegt nichts — die Schwelle darf
# darum grob in der Mitte stehen und braucht kein Feintuning.
_SPALTE_PT = 6.0
# Der Zeitraum einer Position („01.08.24 - 30.08.24") steht zwischen Menge und
# Beträgen. Er muss VOR der Spaltentrennung weg: er sitzt nur acht Punkte neben
# der Menge und klebte sonst mit ihr in einer Zelle, womit die letzte Zelle
# nicht mehr verlässlich der Betrag wäre.
#
# Der Bindestrich darf am Datum kleben. In den vorliegenden Rechnungen steht er
# zwei Punkte daneben und ist ein eigenes Wort — schliesst sich diese Lücke
# (andere Schrift, anderer Satz), liefert PyMuPDF „01.08.24-" als ein Wort, und
# ohne diese Duldung bliebe genau dieses Bruchstück als Zelle stehen.
_ZEITRAUM = re.compile(r"^-?\d{1,2}\.\d{1,2}\.\d{2}-?$")


def pdf_spalten(pfad: Path | str) -> str:
    """Textebene als Spaltentext: eine Zeile je Tabellenzeile, Zellen mit Tabulator."""
    with fitz.open(pfad) as doc:
        return "\n".join(_seite_spalten(seite) for seite in doc)


def _seite_spalten(seite) -> str:  # noqa: ANN001 — fitz.Page, nur hier gebraucht
    """Eine Seite als Spaltentext."""
    # Nach Grundlinie, dann nach linker Kante: erst danach steht jede Zeile in
    # Leserichtung. Ohne die zweite Sortierung mischen sich Kopfzeilen, deren
    # Blöcke das PDF in anderer Reihenfolge ablegt.
    worte = sorted(seite.get_text("words"), key=lambda w: (round(w[3], 1), w[0]))
    zeilen: list[list] = []
    letzte_y: float | None = None
    for w in worte:
        if letzte_y is None or abs(w[3] - letzte_y) > _ZEILE_PT:
            zeilen.append([])
            letzte_y = w[3]
        zeilen[-1].append(w)

    ausgabe: list[str] = []
    for roh in zeilen:
        zeile = _ohne_zeitraum(sorted(roh, key=lambda w: w[0]))
        if not zeile:
            continue
        zellen: list[list[str]] = [[zeile[0][4]]]
        # strict=False ist hier die Absicht: gepaart wird jedes Wort mit seinem
        # rechten Nachbarn, die letzte Paarung fällt weg.
        for links, rechts in zip(zeile, zeile[1:], strict=False):
            if rechts[0] - links[2] > _SPALTE_PT:
                zellen.append([])
            zellen[-1].append(rechts[4])
        ausgabe.append("\t".join(" ".join(z) for z in zellen))
    return "\n".join(ausgabe)


def _ohne_zeitraum(zeile: list) -> list:
    """Datumsangaben und die Bindestriche dazwischen aus einer Zeile werfen."""
    behalten: list = []
    i = 0
    while i < len(zeile):
        j = i
        hat_datum = False
        while j < len(zeile) and (_ZEITRAUM.match(zeile[j][4]) or zeile[j][4] == "-"):
            hat_datum = hat_datum or bool(_ZEITRAUM.match(zeile[j][4]))
            j += 1
        if hat_datum:
            i = j
            continue
        behalten.append(zeile[i])
        i += 1
    return behalten
