"""Kein Wert aus einem echten Beleg darf im Repo stehen.

Die Regel lautet: Testdaten werden ERFUNDEN. Beträge, Kunden-, Vertrags- und
Versicherungsnummern aus den Unterlagen des Nutzers gehören in die Datenbank
und in die Dateien ausserhalb dieses Repos — nicht in Code, Kommentar, Test
oder Dokumentation.

Warum es diese Prüfung braucht: die Regel wurde dreimal verletzt, und zwar
nicht aus Nachlässigkeit, sondern beim *Erklären*. Ein Kommentar wollte
begründen, warum eine Zahl verdächtig war — und schrieb sie dafür hin. Ein
anderer belegte eine Messung mit den gemessenen Beträgen. Jedes Mal war die
Absicht gut und das Ergebnis dasselbe: die Zahl stand danach im Repo.

Was hier NICHT geprüft werden kann: ob ein Betrag echt ist. Das weiss nur der
Nutzer. Geprüft wird deshalb das, was maschinell entscheidbar ist — Muster, die
es in erfundenen Testdaten nicht zu geben braucht, und Formulierungen, die
einen abgelesenen Wert ankündigen.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]

#: Durchsucht werden Code, Vorlagen, Tests und Doku — alles, was mitgeliefert
#: wird. Nicht die Datenverzeichnisse: dort GEHÖREN echte Werte hin.
BEREICHE = ("src", "tests", "docs", "scripts", "README.md")
ENDUNGEN = {".py", ".html", ".md", ".css", ".js", ".json", ".toml"}

#: Nichts. Diese Datei nahm sich früher selbst aus — und trug dann prompt
#: einen echten Betrag im Kommentar, der das Verbot erklären sollte. Genau der
#: Fall, den ihr eigener Kopf beschreibt. Wer sich ausnimmt, prüft sich nicht.
AUSGENOMMEN: set[str] = set()


def _dateien() -> list[Path]:
    out: list[Path] = []
    for bereich in BEREICHE:
        p = WURZEL / bereich
        if p.is_file():
            out.append(p)
            continue
        for datei in p.rglob("*"):
            if (datei.is_file() and datei.suffix in ENDUNGEN
                    and datei.name not in AUSGENOMMEN
                    and ".venv" not in datei.parts
                    and "node_modules" not in datei.parts):
                out.append(datei)
    return out


# --------------------------------------------------------------------------
# 1. Eindeutige Kennungen
# --------------------------------------------------------------------------

#: AHV-Nummer (756.xxxx.xxxx.xx) — es gibt keinen Grund, eine im Repo zu haben.
#: Auch keine erfundene: sie sähe von einer echten nicht zu unterscheiden aus,
#: und der Bank-Import braucht sie nirgends.
_AHV = re.compile(r"\b756\.\d{4}\.\d{4}\.\d{2}\b")

# KEINE IBAN-Prüfung. Sie liesse sich nicht ehrlich schreiben: die Testdateien
# des CAMT-Imports brauchen IBAN, und ob eine davon echt ist, sieht man ihr
# nicht an. Der einzige Vergleich, der entscheiden könnte, wäre der gegen die
# echte — und die stünde dann in dieser Datei. Ein Wächter, der das Leck erst
# schafft, das er verhindern soll, ist keiner.


def test_keine_ahv_nummer_im_repo() -> None:
    treffer = [
        f"{d.relative_to(WURZEL)}: {m.group(0)}"
        for d in _dateien()
        for m in _AHV.finditer(d.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not treffer, "AHV-Nummern gehören nicht ins Repo:\n  " + "\n  ".join(treffer)


# --------------------------------------------------------------------------
# 2. Formulierungen, die einen abgelesenen Wert ankündigen
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 3. Abgleich gegen die WIRKLICH von Hand gelesenen Werte
# --------------------------------------------------------------------------
#
# Der naheliegende Wächter — „eine Zahl kurz hinter dem Wort «gemessen»" —
# wurde gebaut und wieder verworfen. Nachgemessen verfehlte er zwei der drei
# tatsächlichen Verstösse (im Kommentar verlieren Beträge ihre Formatierung und
# sehen aus wie jede andere Zahl) und meldete dafür Pixelbreiten, Kontrastwerte
# und erfundene Testeingaben. Ein Wächter, der öfter irrt als trifft, wird
# abgeschaltet — dann ist er schlechter als keiner.
#
# Dieser hier rät nicht: er vergleicht gegen die Datei, in der die von Hand aus
# den Belegen gelesenen Werte tatsächlich stehen. Sie liegt bewusst AUSSERHALB
# des Repos. Fehlt sie (NAS, fremder Rechner, CI), wird übersprungen — dort
# gibt es nichts zu vergleichen und ein erfundener Vergleich wäre wertlos.

#: Die Datei mit den hand-gelesenen Werten, eine Ebene über dem Repo.
_HANDGELESEN = WURZEL.parent / "verlaeufe_manuell.json"


def _echte_werte() -> set[str]:
    """Jeder Betrag aus der Hand-Datei, in beiden Schreibweisen.

    Beide, weil derselbe Wert im Repo als ``98765.40`` oder als ``98'765.40``
    auftauchen würde — je nachdem, ob er aus dem Code oder vom Bildschirm kommt.
    """
    import json

    daten = json.loads(_HANDGELESEN.read_text(encoding="utf-8"))
    roh: set[str] = set()
    for b in daten.get("befunde", []):
        if isinstance(b.get("wert"), str):
            roh.add(b["wert"])
        for v in (b.get("extras") or {}).values():
            if isinstance(v, str):
                roh.add(v)

    out: set[str] = set()
    for w in roh:
        try:
            zahl = Decimal(w)
        except (InvalidOperation, ValueError):
            continue
        # Unter 1000 ist ein Betrag als Zufallstreffer zu erwarten („480" steht
        # in jedem zweiten Testfall). Erst darüber wird ein Treffer aussagekräftig.
        if abs(zahl) < 1000:
            continue
        ganz = f"{zahl:.2f}"
        out.add(ganz)
        out.add(ganz.rstrip("0").rstrip("."))
        out.add(f"{zahl:,.2f}".replace(",", "'"))
    return out


@pytest.mark.skipif(not _HANDGELESEN.is_file(),
                    reason="verlaeufe_manuell.json liegt nicht daneben — nichts zu vergleichen")
def test_kein_handgelesener_wert_im_repo() -> None:
    """Kein Betrag aus den Belegen des Nutzers steht im Repo.

    Exakt statt heuristisch: verglichen wird gegen die Werte, die wirklich von
    Hand aus den Unterlagen gelesen wurden. Ein Treffer ist damit kein Verdacht,
    sondern ein Befund.
    """
    werte = _echte_werte()
    assert werte, "Vorbedingung: die Hand-Datei enthält vergleichbare Beträge"

    treffer: list[str] = []
    for d in _dateien():
        text = d.read_text(encoding="utf-8", errors="replace")
        treffer += [f"{d.relative_to(WURZEL)}: {w}" for w in werte if w in text]

    assert not treffer, (
        "Diese Beträge stammen aus den Belegen des Nutzers und gehören nicht ins "
        "Repo:\n  " + "\n  ".join(sorted(set(treffer)))
        + "\n\nSie gehören in die Datenbank und in verlaeufe_manuell.json. Im "
          "Code steht die Mechanik, nicht der Wert."
    )
