"""Jeder Aufklapper ist ein Tippziel — und zwar nach Bauform, nicht nach Klasse.

Ein ``<summary>`` ist weder Knopf noch Link. Der 44px-Wächter zählte Klassen
auf, und ein Aufklapper fiel darum durch, bis jemand ihn eintrug. Das ist
viermal passiert — ``.lohn-kopf`` (28px), das Zeilenmenü (30px), die
Budget-Gruppen (24px) und zuletzt ein klassenloses ``<summary>`` auf der
Verläufe-Seite (24px), das sich überhaupt nicht eintragen liess.

Die Liste war der Fehler. Der Wächter greift jetzt an ``details > summary``:
wer aufklappt, ist ein Tippziel, unabhängig davon, wie er heisst. Von den
``<summary>`` der App tragen 14 gar keine Klasse — sie waren über eine Liste
nie erreichbar.

Dieser Test hält die neue Form fest und prüft die zwei Bedingungen, unter denen
sie wirkt: die Regel muss am Element hängen, und jedes ``<summary>`` muss
tatsächlich in einem ``<details>`` stehen (sonst greift der Kindselektor nicht).
"""

from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
CSS = (WURZEL / "src" / "moneten" / "static" / "css" / "theme.css").read_text(encoding="utf-8")
TEMPLATES = WURZEL / "src" / "moneten" / "templates"


def _waechter_selektoren() -> str:
    """Der Selektorkopf der 44px-Regel, ohne Kommentare.

    Kommentare raus: in ihnen stehen Klassennamen als Begründung, und der Test
    bliebe grün, wenn der Selektor selbst verschwindet — an einem anderen
    Wächter genau so passiert.
    """
    block = CSS[CSS.index("Touch-Ziele"):]
    block = block[:block.index("min-height: 44px;")]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


def test_der_waechter_greift_am_element_nicht_an_klassen():
    """Ohne diesen Selektor fällt jedes klassenlose ``<summary>`` wieder durch."""
    assert "details:not(.rowmenu) > summary" in _waechter_selektoren(), (
        "Der 44px-Wächter erfasst Aufklapper nicht mehr nach Bauform. Damit "
        "bekommt jedes <summary> ohne passende Klasse wieder nur seine Texthöhe."
    )


def test_jedes_summary_steht_in_einem_details():
    """Der Kindselektor greift nur, wenn das ``<details>`` wirklich da ist.

    Ein ``<summary>`` ausserhalb eines ``<details>`` ist zwar ohnehin ungültiges
    HTML, aber es hätte hier eine stille Folge: es bekäme kein Tippziel, und
    kein Browser würde sich beschweren.
    """
    fehler: list[str] = []
    for datei in sorted(TEMPLATES.rglob("*.html")):
        text = datei.read_text(encoding="utf-8")
        if "<summary" not in text:
            continue
        tiefe = 0
        for treffer in re.finditer(r"<details\b|</details>|<summary\b", text):
            marke = treffer.group(0)
            if marke.startswith("<details"):
                tiefe += 1
            elif marke == "</details>":
                tiefe -= 1
            elif tiefe <= 0:
                zeile = text.count("\n", 0, treffer.start()) + 1
                fehler.append(f"{datei.relative_to(TEMPLATES)}:{zeile}")
    assert not fehler, (
        "Diese <summary> stehen nicht in einem <details> und bekommen deshalb "
        f"kein Tippziel: {fehler}"
    )


def test_das_zeilenmenue_bleibt_ausgenommen():
    """Die einzige Ausnahme — und sie holt sich ihre 44px auf anderem Weg.

    Das Zeilenmenü ist absichtlich klein (es sitzt in einer Buchungszeile). Eine
    Mindesthöhe von 44px würde jede Zeile aufblähen; stattdessen zieht ein
    ``::after`` die Trefferfläche auf, ohne die Zeile höher zu machen. Fällt das
    weg, ist das Menü ein 30px-Ziel — und zwar unbemerkt, weil der allgemeine
    Wächter es ja ausnimmt.
    """
    block = CSS[CSS.index(".rowmenu > summary::after"):]
    block = block[:block.index("}")]
    assert "height: 44px" in block, (
        "Das Zeilenmenü ist vom 44px-Wächter ausgenommen, holt sich seine "
        "Trefferfläche aber nicht mehr über ::after."
    )
