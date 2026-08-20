"""Das Zeilenmenü ist auf jeder Buchungszeile — und mass 30×30 px.

Es ist der Weg zu Kategorie, Bearbeiten und Löschen. Der 44px-Wächter zählt
Bedienelemente als KLASSEN auf; er nennt ``.rowmenu-item`` (die Einträge IM
Menü), nicht das ``<summary>``, das es öffnet. Damit fiel ausgerechnet der Knopf
durch, den man zuerst treffen muss.

Gemessen im Browser bei 800px (Fold aufgeklappt): ein Tipp 16px neben der Mitte
ging daneben. Nach der Korrektur trifft er aus allen vier Richtungen, und die
Zeilenhöhe blieb bei 72px — die sichtbare Grafik ist weiterhin 30px.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
       / "theme.css").read_text(encoding="utf-8")


def _regel(selektor: str) -> str:
    """Der Rumpf der ersten Regel zu diesem Selektor — ohne Kommentare.

    Kommentare raus, bevor gesucht wird: sonst hält eine Begründung im Text den
    Test grün, während der Selektor längst weg ist (an einem anderen Wächter
    nachgemessen).
    """
    ohne = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    i = ohne.index(selektor)
    return ohne[i:ohne.index("}", i)]


def test_das_zeilenmenue_hat_eine_44px_flaeche():
    """Die Fläche kommt per Pseudoelement — 44px sichtbar machten jede Zeile höher."""
    regel = _regel(".rowmenu > summary::after")
    assert "44px" in regel, f"keine 44px-Fläche: {regel!r}"
    assert "position: absolute" in regel


def test_die_flaeche_ist_zentriert():
    """Sonst zöge sie nur nach einer Seite und die andere bliebe knapp."""
    regel = _regel(".rowmenu > summary::after")
    assert "translate(-50%, -50%)" in regel
    assert "left: 50%" in regel and "top: 50%" in regel


def test_die_regel_gilt_fuer_finger_und_schmale_schirme():
    """Am Zeigegerät mit Maus ist die grosse Fläche unnötig — und im Weg."""
    ohne = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    i = ohne.index(".rowmenu > summary::after")
    davor = ohne[:i]
    letzte_query = davor.rindex("@media")
    assert "pointer: coarse" in davor[letzte_query:i]
    assert "max-width: 820px" in davor[letzte_query:i], (
        "Ohne die Breiten-Hälfte fällt das aufgeklappte Fold (800px) durch: "
        "dort meldet der Browser je nach Modus einen feinen Zeiger."
    )


def test_der_knopf_bleibt_sichtbar_klein():
    """Die sichtbare Grafik darf nicht mitwachsen — sonst wird die Zeile höher."""
    regel = _regel(".rowmenu > summary {")
    assert "min-height: 44px" not in regel
