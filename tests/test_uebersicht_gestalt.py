"""Zusagen der Übersicht, die nur im Stylesheet oder im Skript stehen.

Vier Stellen liessen sich nachgemessen spurlos entfernen, ohne dass irgendetwas
rot wurde — jede von ihnen trägt eine Entscheidung, die beim nächsten Umbau
niemand mehr rekonstruieren könnte:

* der Abstand der allein stehenden Kassensturz-Erinnerung (sie vertritt eine
  Karte und muss deren Abstand tragen),
* die Trennlinie im Kopf der ``.sunken``-Karte („Grösste Ausgaben" liest sich
  als Paar mit „Geldfluss" — zwei verschieden gebaute Köpfe lesen sich als zwei
  verschiedene Bauteile),
* das 44px-Tippziel der Erinnerung,
* der Griff, der den Kachel-Tooltip am Finger überhaupt öffnet.

Geprüft wird ohne Browser: ``theme.css`` und ``app.js`` werden geparst. Das
fängt nicht jede Überlagerung, aber jede dieser vier Löschungen. Vorbild und
Mini-Scanner sind dieselben wie in ``test_dock.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "moneten" / "static"
THEME = STATIC / "css" / "theme.css"
APP_JS = STATIC / "js" / "app.js"


def _bloecke(css: str) -> list[tuple[str, str]]:
    """``[(media_bedingung, rumpf)]`` — ausserhalb jeder Query steht ``""``."""
    treffer: list[tuple[str, str]] = []
    rest: list[str] = []
    i = 0
    while True:
        m = re.compile(r"@(?:media|container)([^{]*)\{").search(css, i)
        if not m:
            rest.append(css[i:])
            break
        rest.append(css[i:m.start()])
        tiefe, j = 1, m.end()
        while j < len(css) and tiefe:
            if css[j] == "{":
                tiefe += 1
            elif css[j] == "}":
                tiefe -= 1
            j += 1
        treffer.append((m.group(1).strip(), css[m.end():j - 1]))
        i = j
    treffer.append(("", "".join(rest)))
    return treffer


def _regeln() -> list[tuple[str, str, dict[str, str]]]:
    """``[(bedingung, selektorliste, {eigenschaft: wert})]`` der ganzen Datei.

    Kommentare fliegen VOR dem Zerlegen raus: in ihnen stehen Selektoren als
    Begründung, und ein Test, der sie mitliest, bleibt grün, wenn nur noch die
    Begründung übrig ist.
    """
    css = re.sub(r"/\*.*?\*/", " ", THEME.read_text(encoding="utf-8"), flags=re.S)
    aus: list[tuple[str, str, dict[str, str]]] = []
    for bedingung, rumpf in _bloecke(css):
        for selektor, block in re.findall(r"([^{}]+)\{([^{}]*)\}", rumpf):
            deklarationen: dict[str, str] = {}
            for zeile in block.split(";"):
                if ":" not in zeile:
                    continue
                name, _, wert = zeile.partition(":")
                deklarationen[name.strip()] = wert.strip()
            aus.append((bedingung, " ".join(selektor.split()), deklarationen))
    return aus


def _deklarationen(selektor_teil: str, *, bedingung: str = "") -> dict[str, str]:
    """Was die Regeln mit diesem Selektor setzen — spätere überschreiben frühere.

    Gleiche Spezifität vorausgesetzt, entspricht das der Kaskade.
    """
    zusammen: dict[str, str] = {}
    for bed, selektor, deklarationen in _regeln():
        if bed == bedingung and selektor_teil in selektor:
            zusammen.update(deklarationen)
    return zusammen


def _laenge(wert: str) -> str:
    """Löst ein einfaches ``var(--x)`` gegen ``:root`` auf, sonst unverändert."""
    m = re.fullmatch(r"var\((--[\w-]+)\)", wert.strip())
    if not m:
        return wert.strip()
    wurzel = _deklarationen(":root")
    return wurzel.get(m.group(1), wert).strip()


def test_alleinstehende_erinnerung_traegt_den_abstand_ihrer_karte() -> None:
    """Sie steht anstelle einer Karte und muss deren Abstand mitbringen.

    Ihr eigener Innenabstand ist enger; ohne die Regel klebte sie an der
    nächsten Karte. Verglichen wird gegen ``.mb-4`` — den Abstand, den jede
    Karte der Übersicht trägt. Eine eigene Zahl hier wäre eine zweite Quelle
    für dieselbe Entscheidung.
    """
    solo = _deklarationen(".mix-erinnerung.is-solo").get("margin-bottom")
    karte = _deklarationen(".mb-4").get("margin-bottom")
    assert solo, "Die allein stehende Erinnerung hat keinen eigenen Abstand mehr"
    assert karte, "Vorbedingung: .mb-4 setzt einen Abstand"
    assert _laenge(solo) == _laenge(karte), (
        f"Die Erinnerung trägt {solo}, die Karte, die sie vertritt, {karte}"
    )


def test_beide_kartenkoepfe_der_uebersicht_sind_gleich_gebaut() -> None:
    """„Geldfluss" und „Grösste Ausgaben" zeigen denselben Monat — als Paar.

    Der Kopf der ``.sunken``-Karte bekommt kein farbiges Band (bg-sunken auf
    bg-sunken wäre unsichtbar), aber denselben Zuschnitt und dieselbe
    Trennlinie. Ohne die Regel war er der einzige Kartenkopf der Seite ohne
    Unterstrich, flach und ohne Innenabstand.
    """
    band = _deklarationen(".card:not(.sunken) > .card-head:first-child")
    sunken = _deklarationen(".card.sunken > .card-head:first-child")
    assert sunken, "Der Kopf der .sunken-Karte hat keine eigene Regel"
    for eigenschaft in ("margin", "padding", "border-bottom"):
        assert sunken.get(eigenschaft) == band.get(eigenschaft), (
            f"{eigenschaft}: .sunken hat {sunken.get(eigenschaft)!r}, "
            f"der Kopf darüber {band.get(eigenschaft)!r}"
        )


def test_erinnerung_ist_ein_tippziel() -> None:
    """Sie ist ein ``<a>`` ohne ``.btn`` und fällt durch die Liste der Tippziele.

    Ihre 44px standen zuvor an ihrer eigenen Regel — dort hielt sie kein Test,
    und sie liessen sich entfernen, ohne dass etwas rot wurde. Jetzt stehen sie
    dort, wo alle anderen Tippziele stehen.
    """
    wort = re.compile(r"(?<![\w-])\.mix-erinnerung(?![\w-])")
    treffer = [
        (bedingung, selektor)
        for bedingung, selektor, deklarationen in _regeln()
        if deklarationen.get("min-height") == "44px" and wort.search(selektor)
    ]
    assert treffer, (
        "Die Kassensturz-Erinnerung steht in keiner 44px-Regel — sie fällt auf "
        "die Höhe ihres Textes zurück, ohne dass es jemandem auffällt"
    )
    assert any("coarse" in bedingung for bedingung, _ in treffer), (
        f"Die 44px gelten nicht am Finger: {treffer}"
    )


def test_kachel_gibt_ihren_tooltip_auch_am_finger_her() -> None:
    """Die Kachel lässt weg, was nicht hineinpasst — vollständig ist nur der Tooltip.

    Bei schmalen Kacheln fehlt der Name, bei flachen zusätzlich der Betrag; beides
    steht dann ausschliesslich in ``data-tip``, und der erscheint per ``:hover``.
    Am Handy gibt es kein Hover — dort war die weggelassene Hälfte gar nicht zu
    bekommen. Ohne diesen Test lässt sich der Griff wieder aushängen, und die
    Karte sieht danach genauso aus wie vorher.
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert "function initTreemapTipp(" in js, "Der Griff auf die Kachel ist weg"
    # NUR im Rumpf von boot(): der HTMX-Nachbinder ruft dieselbe Funktion auf,
    # und eine Suche ueber die ganze Datei bliebe gruen, wenn allein der
    # Erstaufruf fehlt — dann waere die Kachel bis zum ersten Swap ungebunden.
    boot = js[js.index("function boot()"):]
    assert "initTreemapTipp(" in boot[:boot.index("\n  }")], (
        "initTreemapTipp wird beim Start nie aufgerufen — die Kacheln bleiben ungebunden"
    )
    start = js.index("function initTreemapTipp(")
    rumpf = js[start:js.index("\n  }", start)]
    assert 'pointerType === "mouse"' in rumpf, (
        "Der Griff greift auch mit der Maus — dort schiebt sich der Kasten vor "
        "die Kachel, die man gerade anklickt"
    )
    assert "is-tipp" in rumpf, "Der Griff setzt die Klasse nicht, die den Kasten zeigt"
