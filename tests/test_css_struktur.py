"""Die Stilblätter müssen sich parsen lassen — sonst hört die App leise auf zu wirken.

**Warum es das gibt.** Beim Umbau der Tippziele blieb eine schliessende Klammer
stehen und eine andere fehlte. Beides zusammen verschob die Verschachtelung so,
dass ab dieser Stelle NICHTS mehr galt: der Tippziel-Wächter, die
Zahlen-Ziffern, die Diagrammbalken — alles ab Zeile 2875 war wirkungslos. Im
Browser sah die Seite dabei fast normal aus, und kein Test wurde rot.

Ein Stilblatt scheitert nicht mit einer Fehlermeldung. Der Browser überliest,
was er nicht versteht, und arbeitet weiter — deshalb muss die Struktur hier
geprüft werden und nicht am Bildschirm.

Geprüft wird dreierlei, jedes davon ein tatsächlich passierter Fehler:

* **Klammern gehen auf und wieder zu.** Eine zu viel oder eine zu wenig
  verschiebt alles Folgende.
* **Kein Selektor endet mit einem Komma vor einer At-Regel.** So sah der erste
  Fehler dieser Art aus: ``.vl-xachse > span,`` gefolgt von ``@media`` — die
  Regel darüber verschwand mitsamt der Media-Abfrage darunter.
* **Keine Media-Abfrage ohne Inhalt.** Sie ist das Anzeichen dafür, dass beim
  Verschieben eines Blocks der Rumpf zurückblieb.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BLAETTER = sorted((Path(__file__).resolve().parents[1] / "src/moneten/static/css").glob("*.css"))


def _ohne_kommentare(roh: str) -> tuple[str, list[int]]:
    """Text ohne ``/* … */`` plus die Zeilennummer je verbliebenem Zeichen."""
    zeichen: list[str] = []
    zeilen: list[int] = []
    i, n, z = 0, len(roh), 1
    while i < n:
        if roh.startswith("/*", i):
            ende = roh.find("*/", i + 2)
            ende = n if ende < 0 else ende + 2
            z += roh.count("\n", i, ende)
            i = ende
            continue
        zeichen.append(roh[i])
        zeilen.append(z)
        if roh[i] == "\n":
            z += 1
        i += 1
    return "".join(zeichen), zeilen


def offene_klammern(quelle: str) -> list[str]:
    """Befunde zur Klammerung: was bleibt offen, was schliesst ins Leere."""
    text, zeilen = _ohne_kommentare(quelle)
    stapel: list[int] = []
    befunde: list[str] = []
    for k, ch in enumerate(text):
        if ch == "{":
            stapel.append(zeilen[k])
        elif ch == "}":
            if stapel:
                stapel.pop()
            else:
                befunde.append(f"schliessende Klammer ohne oeffnende auf Zeile {zeilen[k]}")
    befunde += [f"Block bleibt offen, geoeffnet auf Zeile {z}" for z in stapel]
    return befunde


def haengende_kommata(quelle: str) -> list[str]:
    """Ein Komma darf nie das Letzte vor ``@`` oder ``}`` sein.

    ``.vl-xachse > span,`` gefolgt von ``@media`` ist syntaktisch eine
    unvollstaendige Selektorliste: der Browser verwirft die Regel UND den Block
    dahinter. Genau so verschwand die Positionierung der Achsenbeschriftung.
    """
    text, zeilen = _ohne_kommentare(quelle)
    return [f"Selektorliste endet mit Komma vor '{t.group(1)}' auf Zeile {zeilen[t.start()]}"
            for t in re.finditer(r",\s*(@|\})", text)]


def leere_media_abfragen(quelle: str) -> list[str]:
    text, zeilen = _ohne_kommentare(quelle)
    return [f"leere Media-Abfrage auf Zeile {zeilen[t.start()]}"
            for t in re.finditer(r"@media[^{]*\{\s*\}", text)]


@pytest.mark.parametrize("blatt", BLAETTER, ids=lambda p: p.name)
def test_klammern_gehen_auf_und_zu(blatt: Path) -> None:
    befunde = offene_klammern(blatt.read_text(encoding="utf-8"))
    assert not befunde, f"{blatt.name}: {befunde}"


@pytest.mark.parametrize("blatt", BLAETTER, ids=lambda p: p.name)
def test_kein_selektor_endet_im_nichts(blatt: Path) -> None:
    befunde = haengende_kommata(blatt.read_text(encoding="utf-8"))
    assert not befunde, f"{blatt.name}: {befunde}"


@pytest.mark.parametrize("blatt", BLAETTER, ids=lambda p: p.name)
def test_keine_leere_media_abfrage(blatt: Path) -> None:
    befunde = leere_media_abfragen(blatt.read_text(encoding="utf-8"))
    assert not befunde, f"{blatt.name}: {befunde}"


def test_waechter_findet_die_drei_echten_brueche() -> None:
    """Der Prüfer selbst wird geprüft — an allen drei Fehlerformen.

    Ohne diese Gegenprobe koennte er stumm alles durchwinken; genau das tat die
    Testsuite waehrend des Bruchs.
    """
    assert offene_klammern(".a { color: red; }\n}\n.b { color: blue; }\n")
    assert offene_klammern("@media (max-width: 1px) {\n  .a { color: red; }\n")
    assert not offene_klammern("@media (max-width: 1px) {\n  .a { color: red; }\n}\n")

    assert haengende_kommata(".a > span,\n@media (max-width: 1px) { .b { color: red } }\n")
    assert not haengende_kommata(".a > span,\n.b { color: red }\n")

    assert leere_media_abfragen("@media (max-width: 1px) {\n}\n")
    assert not leere_media_abfragen("@media (max-width: 1px) { .a { color: red } }\n")
