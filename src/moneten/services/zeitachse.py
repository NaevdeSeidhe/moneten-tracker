"""Beschriftete Zeitachse für die Verlaufsdiagramme.

Beide Diagramme der Verlaufsseite — die Linie und die gestapelten Balken —
zeigten am unteren Rand genau zwei Angaben: den ersten und den letzten Wert.
Zwischen ihnen lagen bis zu zwei Jahre ohne eine einzige Marke; wo ein Balken
sass, liess sich nur zählen.

Zwei Regeln, beide gemessen und nicht geraten:

**Die Achse endet HEUTE, nicht am letzten Wert.** Endete sie am letzten Wert,
sah eine Reihe, deren jüngster Eintrag von 2023 stammt, genauso aktuell aus wie
eine von gestern — die Kurve lief in beiden Fällen bis zum rechten Rand. Der
leere Raum rechts IST die Aussage: hier fehlt etwas.

**Die Marken sitzen auf runden Daten**, nicht auf den Werten. Ein Raster, das
sich nach den Werten richtet, verschiebt sich mit jedem neuen Wert; eines aus
Jahres- und Halbjahresanfängen bleibt stehen und ist mit einem Blick zu lesen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from moneten.dates import add_months

# Erlaubte Schrittweiten in Monaten. Alles dazwischen (5 Monate, 7 Monate) ergibt
# Marken auf krummen Daten — „05.2025, 10.2025, 03.2026" liest sich schlechter
# als dieselbe Achse mit Halbjahresschritten.
_SCHRITTE_MONATE = (1, 2, 3, 6, 12, 24, 60)

# Ab dieser Schrittweite trägt die Marke nur noch die Jahreszahl. Bei
# Jahresschritten ist der Monat immer Januar und sagt nichts.
_NUR_JAHR_AB = 12

# Kleinster Abstand zweier Marken in Prozent der Achsenbreite. Bei 375px sind
# das rund 60px; eine Marke „01.2025" ist knapp 48px breit. Enger stossen die
# Zeichen aneinander.
_MIN_ABSTAND = 16.0

# Mehr Marken passen am Handy nicht nebeneinander.
_MAX_MARKEN = 5


@dataclass(frozen=True)
class Marke:
    """Eine Achsenmarke: Position in Prozent und ihre Beschriftung."""

    x: float
    text: str


def _erste_marke(von: date, schritt: int) -> date:
    """Der erste Rasterpunkt ab ``von``.

    Das Raster hängt am Jahresanfang und nicht am ersten Wert: sonst wanderte
    die ganze Achse, sobald ein Wert früher nachgetragen wird.
    """
    if schritt >= _NUR_JAHR_AB:
        jahre = schritt // 12
        jahr = von.year if von.month == 1 and von.day == 1 else von.year + 1
        while (jahr - von.year) % jahre:
            jahr += 1
        return date(jahr, 1, 1)
    monat_index = (von.month - 1) // schritt * schritt
    kandidat = date(von.year, monat_index + 1, 1)
    return kandidat if kandidat >= von else add_months(kandidat, schritt)


def _bauen(von: date, bis: date, schritt: int) -> list[Marke]:
    """Die Marken fuer EINE Schrittweite — ohne Ruecksicht darauf, ob es zu viele sind."""
    format_ = "%Y" if schritt >= _NUR_JAHR_AB else "%m.%Y"
    spanne = (bis - von).days
    ergebnis = [Marke(0.0, von.strftime(format_))]
    tag = _erste_marke(von, schritt)
    while tag <= bis:
        x = round((tag - von).days / spanne * 100, 2)
        text = tag.strftime(format_)
        if (x - ergebnis[-1].x >= _MIN_ABSTAND and x <= 100 - _MIN_ABSTAND
                and text != ergebnis[-1].text):
            ergebnis.append(Marke(x, text))
        tag = add_months(tag, schritt)
    schluss = bis.strftime(format_)
    if 100 - ergebnis[-1].x >= _MIN_ABSTAND and schluss != ergebnis[-1].text:
        ergebnis.append(Marke(100.0, schluss))
    return ergebnis


def marken(von: date, bis: date, *, max_marken: int = _MAX_MARKEN) -> list[Marke]:
    """Achsenmarken zwischen ``von`` und ``bis`` — beide Raender eingeschlossen.

    Der Anfang traegt IMMER eine Marke, auch wenn er nicht auf dem Raster liegt:
    ohne sie begaenne die Achse mit einer Luecke, und der erste Wert stuende
    ueber dem Nichts. Das Ende bekommt eine, wenn es weit genug von der letzten
    Rastermarke entfernt ist und nicht dieselbe Beschriftung traegt.

    **Die Schrittweite wird am Ergebnis gewaehlt, nicht geschaetzt.** Eine
    Rechnung „Monate durch Schritt" traf daneben, sobald Mindestabstand oder
    doppelte Beschriftungen Marken wegfielen: eine Achse ueber 24 Monate bekam
    Jahresschritte und damit drei Marken, obwohl fuenf Halbjahresmarken
    hineingepasst haetten. Jetzt wird jede Schrittweite gebaut und die feinste
    genommen, die passt.
    """
    if bis <= von:
        return [Marke(0.0, von.strftime("%m.%Y"))]
    letzte: list[Marke] = []
    for schritt in _SCHRITTE_MONATE:
        gebaut = _bauen(von, bis, schritt)
        letzte = gebaut
        if len(gebaut) <= max_marken:
            return gebaut
    return letzte[:max_marken]


def bis_heute(letzter: date, heute: date) -> date:
    """Rechter Rand der Achse: der spätere von letztem Wert und heute.

    Als eigene Funktion, weil beide Diagramme dieselbe Antwort brauchen und ein
    ``max()`` an zwei Stellen zwei Gelegenheiten wäre, es unterschiedlich zu
    machen.
    """
    return max(letzter, heute)
