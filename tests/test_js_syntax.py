"""Das Browser-Skript muss sich parsen lassen — sonst steht die App still.

**Warum es das gibt.** Für ``theme.css`` prüft ``test_css_struktur.py`` seit
heute die Struktur, nachdem eine überzählige Klammer alles ab dieser Stelle
wirkungslos gemacht hatte. Für ``app.js`` gab es nichts Vergleichbares — dabei
ist die Folge dort härter: ein Syntaxfehler bricht die Auswertung der ganzen
Datei ab, und damit stehen Beleg-Scan, Kategorie-Picker, Suche und das
Zeilenmenü still. Der Server antwortet weiter, die Seite baut sich auf, die
Testsuite bleibt grün. Nur nichts reagiert mehr.

**Der Preis dafür ist eine Sprachgrenze.** ``esprima`` liest ES2017 — kein
``?.``, kein ``??``. Die eine Stelle, die optional chaining benutzte, ist
ausgeschrieben. Das ist kein reiner Verlust: die App läuft auf einem Handy und
soll auch von einem älteren Browser bedient werden können.

Wer hier trotzdem neuere Syntax braucht, tauscht bewusst diesen Wächter gegen
sie — und sollte das dann auch hier vermerken, statt den Test zu löschen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

esprima = pytest.importorskip(
    "esprima",
    reason="esprima fehlt — 'pip install -e .[dev]' installiert ihn mit",
)

JS_ORDNER = Path(__file__).resolve().parents[1] / "src/moneten/static/js"
SKRIPTE = sorted(JS_ORDNER.glob("*.js"))


def test_es_gibt_ueberhaupt_skripte() -> None:
    """Sonst wäre die Prüfung unten eine leere Geste."""
    assert SKRIPTE, f"Keine .js-Dateien in {JS_ORDNER}"


@pytest.mark.parametrize("skript", SKRIPTE, ids=lambda p: p.name)
def test_skript_ist_syntaktisch_gueltig(skript: Path) -> None:
    try:
        esprima.parseScript(skript.read_text(encoding="utf-8"))
    except Exception as fehler:  # esprima wirft eine eigene Error-Klasse
        pytest.fail(f"{skript.name}: {fehler}")


def test_der_waechter_findet_einen_echten_fehler() -> None:
    """Die Gegenprobe — ein Prüfer, der nie anschlägt, prüft nichts.

    Drei Formen, jede eine, die beim Bearbeiten per Skript tatsächlich
    entstehen kann: fehlende Klammer, überzählige Klammer, abgeschnittene
    Zeichenkette.
    """
    def parst(quelle: str) -> bool:
        try:
            esprima.parseScript(quelle)
        except Exception:  # noqa: BLE001 - esprima wirft eine eigene Klasse
            return False
        return True

    assert not parst("function a() { if (x) { return 1; }")   # Klammer fehlt
    assert not parst("function a() { return 1; } }")          # Klammer zu viel
    assert not parst('const s = "unbeendet;\nconst t = 2;')   # Zeichenkette offen
    assert parst("function a() { return 1; }")                # und das Gegenstueck


def test_kein_optional_chaining(  # noqa: D401 - Titel ist die Aussage
) -> None:
    """``?.`` und ``??`` würden den Parser oben aushebeln — und zwar lautlos.

    Ohne diesen Test bliebe die Suite grün, während der Wächter an der ersten
    modernen Zeile abbricht und ab dort nichts mehr prüft.
    """
    for skript in SKRIPTE:
        if skript.name.endswith(".min.js"):
            continue  # Fremdcode; er parst, und umschreiben wuerden wir ihn nie
        text = skript.read_text(encoding="utf-8")
        zeilen = [
            f"{skript.name}:{nr}"
            for nr, zeile in enumerate(text.splitlines(), 1)
            if ("?." in zeile or "??" in zeile) and not zeile.lstrip().startswith("//")
        ]
        assert not zeilen, (
            f"Neuere Syntax in {zeilen} — damit bricht esprima ab und der "
            "Syntax-Waechter prueft ab dort nichts mehr."
        )
