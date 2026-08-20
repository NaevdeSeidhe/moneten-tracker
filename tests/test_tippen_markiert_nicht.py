"""Ein Tipp auf ein Bedienelement muss auslösen, nicht markieren.

Gemeldet am Kamera-Knopf: „manchmal wenn ich auf kamera tippe, icon, wird aus
ausgewaehlt. markiert." Bleibt der Finger den Bruchteil einer Sekunde zu lang
liegen, startet Android die Textauswahl — um die Beschriftung erscheinen
Auswahlgriffe, und der Knopf löst nicht aus. Das ist keine Eigenheit dieses
Knopfes, sondern der Normalfall für jedes Element, dessen Text markierbar ist.

Der Kamera-Knopf fiel zusätzlich durch jede pauschale ``button``-Regel: er ist
ein ``<label>``, weil er das Datei-Feld umschliesst.

``theme.css`` führt zwei Listen darüber, was ein Bedienelement ist — die eine
gibt ihm 44px Trefferfläche, die andere nimmt ihm die Markierbarkeit. Zwei
Listen, die dasselbe meinen, laufen auseinander; diese Tests halten sie
zusammen. Geprüft wird ohne Browser: ``theme.css`` wird geparst.
"""

from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
THEME = WURZEL / "src" / "moneten" / "static" / "css" / "theme.css"
SHELL = WURZEL / "src" / "moneten" / "templates" / "_shell.html"

# Die Abschnittsüberschriften von theme.css sind der Anker — sie stehen dort
# app-weit im selben Format und benennen genau die beiden Listen.
MARKIERUNG = "/* ----------  Ein Tipp ist ein Tipp, keine Textmarkierung  ----------"
TIPPZIEL = "/* ----------  Touch-Ziele  ----------"


def _ohne_kommentare(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _erste_regel_nach(marke: str) -> tuple[list[str], dict[str, str]]:
    """``([selektor, …], {eigenschaft: wert})`` der ersten Regel nach der Überschrift.

    Kommentare werden erst NACH dem Anker entfernt (er ist selbst einer). Eine
    ``@media``-Klammer überspringt das Muster von allein: ihr Rumpf enthält
    geschweifte Klammern, ``[^{}]*`` passt darauf nicht.
    """
    roh = THEME.read_text(encoding="utf-8")
    assert marke in roh, f"Abschnitt {marke!r} steht nicht mehr in theme.css"
    rest = _ohne_kommentare(roh[roh.index(marke) :])
    m = re.search(r"([^{}]+)\{([^{}]*)\}", rest)
    assert m, f"Nach {marke!r} folgt keine Regel"
    selektoren = [s.strip() for s in m.group(1).split(",") if s.strip()]
    deklarationen = {}
    for zeile in m.group(2).split(";"):
        name, _, wert = zeile.partition(":")
        if wert:
            deklarationen[name.strip()] = wert.strip()
    return selektoren, deklarationen


def test_was_gross_genug_zum_tippen_ist_markiert_auch_nicht() -> None:
    """Der Kern: beide Listen beschreiben dasselbe — „das ist ein Bedienelement".

    Die eine sagt es in Pixeln, die andere im Verhalten. Fehlt ein Eintrag in
    der zweiten, ist genau dieser Knopf am Finger wieder markierbar — und das
    fällt niemandem auf, der mit der Maus prüft.
    """
    tippziele, tipp_dekl = _erste_regel_nach(TIPPZIEL)
    markierfrei, mark_dekl = _erste_regel_nach(MARKIERUNG)

    assert tipp_dekl.get("min-height") == "44px", (
        f"Nach {TIPPZIEL!r} steht nicht mehr der 44px-Guard, sondern {tipp_dekl}"
    )
    assert mark_dekl.get("user-select") == "none", (
        f"Nach {MARKIERUNG!r} steht keine user-select-Regel, sondern {mark_dekl}"
    )

    fehlend = [s for s in tippziele if s not in markierfrei]
    assert not fehlend, (
        "Diese Bedienelemente bekommen 44px Trefferfläche, bleiben aber "
        f"markierbar: {fehlend}. Am Finger löst ein etwas zu langer Tipp dort "
        "die Textauswahl aus statt der Aktion."
    )


def test_der_kamera_knopf_faellt_durch_jede_button_regel() -> None:
    """Er ist ein ``<label>``, kein ``<button>`` — genau der gemeldete Fall.

    Damit greift weder ``button`` noch der Tippziel-Guard (60px hat er selbst).
    Ohne den eigenen Eintrag markiert ausgerechnet der Knopf, der am häufigsten
    im Vorbeigehen mit dem Daumen getroffen wird.
    """
    shell = SHELL.read_text(encoding="utf-8")
    assert re.search(r"<label[^>]*class=\"fab\"", shell), (
        "Der Kamera-FAB ist kein <label> mehr — dann prüft dieser Test die "
        "falsche Begründung und gehört überarbeitet."
    )
    markierfrei, _ = _erste_regel_nach(MARKIERUNG)
    assert ".fab" in markierfrei, (
        ".fab fehlt in der Markierungs-Liste. Ein <label> ist kein <button>: "
        "die pauschale Regel greift dort nicht."
    )


def test_die_felder_der_quittung_lassen_sich_antippen_statt_markieren() -> None:
    """Auf dem Kassenzettel öffnet ein Tipp das Eingabefeld.

    Merkt Android das als Markierung, kommt man an Anbieter, Datum, Preis und
    Total nicht mehr heran — und das sind die Werte, die nach dem Scan
    erfahrungsgemäss korrigiert werden müssen.
    """
    markierfrei, _ = _erste_regel_nach(MARKIERUNG)
    assert ".kz [data-edit]" in markierfrei, (
        "Die antippbaren Belegfelder fehlen in der Markierungs-Liste."
    )


def test_eingabefelder_bleiben_markierbar() -> None:
    """Die Rückausnahme ist kein Beiwerk, sondern Bedingung der Regel oben.

    ``user-select`` vererbt sich. ``.kz [data-edit]`` bekommt beim Bearbeiten
    ein ``<input>`` hineingesetzt — ohne Rückausnahme liesse sich der Text
    darin weder markieren noch ersetzen.
    """
    css = _ohne_kommentare(THEME.read_text(encoding="utf-8"))
    regeln = [
        (sel, rumpf) for sel, rumpf in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(r"(?<!-)user-select:\s*text", rumpf)
    ]
    assert regeln, "Keine Regel stellt user-select für Eingabefelder wieder her"
    abgedeckt = {
        teil.strip()
        for sel, _ in regeln
        for teil in sel.split(",")
    }
    for pflicht in ("input", "textarea"):
        assert pflicht in abgedeckt, (
            f"{pflicht} steht in keiner Rückausnahme — der Text darin ist damit "
            f"dort nicht markierbar, wo das Feld in einem Bedienelement steckt."
        )
