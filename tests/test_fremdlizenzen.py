"""Die Lizenz-Offenlegung muss stimmen, nicht bloss vorhanden sein.

Zwei Sorten Fehler soll das hier verhindern:

1. **Eine neue Abhängigkeit, die niemand nennt.** Die Übersicht wurde einmal
   geschrieben und veraltet ab dem nächsten `pyproject`-Eintrag. Ein Paket mit
   fremden Bedingungen, das nirgends steht, ist genau die Lücke, die eine
   Offenlegung schliessen soll.
2. **Ein Lizenztext, der nur verlinkt ist.** Die SIL Open Font License verlangt
   ausdrücklich, dass Lizenz und Copyright-Vermerk die Schrift *begleiten*. Ein
   Verweis ins Netz erfüllt das nicht — und stirbt irgendwann.

Der Test läuft im Arbeitsordner wie im Export: die Übersicht liegt dort als
Vorlage, hier im Wurzelverzeichnis des Repositorys.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
STATIC = WURZEL / "src" / "moneten" / "static"

# Im Export liegt die Datei neben dem README, im Arbeitsordner bei den Vorlagen.
_ORTE = [
    WURZEL / "THIRD-PARTY-NOTICES.md",
    WURZEL.parent / "veroeffentlichen" / "vorlagen" / "THIRD-PARTY-NOTICES.md",
]


def _uebersicht() -> str:
    for pfad in _ORTE:
        if pfad.is_file():
            return pfad.read_text(encoding="utf-8")
    pytest.skip("THIRD-PARTY-NOTICES.md liegt nirgends daneben — nichts abzugleichen")
    raise AssertionError  # pragma: no cover — pytest.skip springt vorher raus


def _direkte_abhaengigkeiten() -> list[str]:
    daten = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    return [
        re.split(r"[<>=\[!;]", d, maxsplit=1)[0].strip()
        for d in daten["project"]["dependencies"]
    ]


def test_jede_direkte_abhaengigkeit_ist_genannt() -> None:
    """Wer ein Paket hinzufügt, muss es auch offenlegen.

    Ohne diesen Test bleibt die Übersicht auf dem Stand des Tages, an dem sie
    entstand. Der nächste `pyproject`-Eintrag wäre dann eine stille Auslassung.
    """
    text = _uebersicht().lower()
    fehlend = [n for n in _direkte_abhaengigkeiten() if n.lower() not in text]
    assert not fehlend, (
        "Diese Abhängigkeiten stehen nicht in THIRD-PARTY-NOTICES.md: "
        f"{', '.join(fehlend)}"
    )


def test_pymupdf_ist_als_agpl_gekennzeichnet() -> None:
    """Die eine Abhängigkeit, die nicht permissiv ist, muss auffallen.

    Sie in einer Tabelle unter zwanzig MIT-Zeilen zu verstecken wäre formal
    korrekt und praktisch nutzlos — wer die App weitergibt, muss es sehen.
    """
    text = _uebersicht()
    assert "PyMuPDF" in text
    agpl_stellen = [z for z in text.splitlines() if "AGPL" in z]
    assert len(agpl_stellen) >= 2, (
        "AGPL wird höchstens einmal erwähnt — zu leicht zu übersehen"
    )
    assert re.search(r"^#+.*AGPL", text, re.MULTILINE), (
        "Kein eigener Abschnitt für die AGPL-Abhängigkeit"
    )


def test_der_ofl_volltext_liegt_bei_der_schrift() -> None:
    """Die OFL verlangt den Text, nicht den Link."""
    ofl = STATIC / "fonts" / "OFL.txt"
    assert ofl.is_file(), "static/fonts/OFL.txt fehlt"
    text = ofl.read_text(encoding="utf-8")

    for abschnitt in ("PREAMBLE", "DEFINITIONS", "PERMISSION & CONDITIONS",
                      "TERMINATION", "DISCLAIMER"):
        assert re.search(rf"^{re.escape(abschnitt)}", text, re.MULTILINE), (
            f"Abschnitt {abschnitt} fehlt — der Text ist nicht vollständig"
        )
    # Der Copyright-Vermerk gehört zur Bedingung, nicht zur Zierde: die
    # Platzhalter-Fassung der OFL-Vorlage würde hier durchfallen.
    assert re.search(r"Copyright.*Poppins", text), "Copyright-Zeile der Schrift fehlt"
    assert "<Copyright Holder>" not in text, "Es ist die Platzhalter-Vorlage, nicht die echte Datei"


def test_die_htmx_lizenz_liegt_beim_skript() -> None:
    """Auch 0BSD will genannt werden — und die Fassung soll nachlesbar sein."""
    lizenz = STATIC / "js" / "htmx-LICENSE.txt"
    assert lizenz.is_file(), "static/js/htmx-LICENSE.txt fehlt"
    text = lizenz.read_text(encoding="utf-8")
    assert "BSD" in text, "Die Datei nennt die Lizenz nicht"
    assert (STATIC / "js" / "htmx.min.js").is_file()


def test_die_schrift_notiz_zeigt_auf_den_volltext() -> None:
    """Vorher stand dort nur eine Netzadresse."""
    notiz = (STATIC / "fonts" / "LIZENZ.md").read_text(encoding="utf-8")
    assert "OFL.txt" in notiz, "LIZENZ.md verweist nicht auf den beiliegenden Volltext"


# ---------------------------------------------------------------------------
# Die mitgelieferte Fassung darf nicht davonlaufen
# ---------------------------------------------------------------------------
def test_die_htmx_fassung_stimmt_mit_der_uebersicht_ueberein() -> None:
    """Wer die Datei tauscht und die Tabelle vergisst, fliegt auf.

    Heute stimmen beide überein, ohne dass irgendetwas das sicherstellt. Eine
    Übersicht, die eine andere Fassung nennt als die ausgelieferte, ist schlimmer
    als keine: sie sieht geprüft aus.
    """
    datei = (STATIC / "js" / "htmx.min.js").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'version:"([\d.]+)"', datei)
    assert m, "In htmx.min.js steht keine Fassungsnummer mehr — Muster prüfen"
    fassung = m.group(1)
    assert fassung in _uebersicht(), (
        f"Ausgeliefert wird htmx {fassung}; THIRD-PARTY-NOTICES.md nennt diese "
        f"Fassung nicht."
    )


def test_die_mitgelieferten_dateien_wurden_kuerzlich_geprueft() -> None:
    """Die einzige Erinnerung, die es für htmx überhaupt gibt.

    ``pip-audit`` sieht nur Python-Pakete; eine Datei im Repository ist für jedes
    Werkzeug unsichtbar. Dieser Test ist deshalb bewusst eine Zeitbombe: er wird
    ohne jede Änderung am Code rot, und zwar genau dann, wenn niemand mehr an
    htmx denkt. Die Meldung nennt den Handgriff, sonst wäre er nur ärgerlich.
    """
    from datetime import date

    text = _uebersicht()
    m = re.search(r"Stand der Prüfung auf Aktualisierungen: (\d{4})-(\d{2})-(\d{2})", text)
    assert m, "Kein Prüfdatum in THIRD-PARTY-NOTICES.md"
    geprueft = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    alter = (date.today() - geprueft).days
    assert alter <= 180, (
        f"Die mitgelieferten Fremd-Dateien (htmx, Poppins) wurden zuletzt vor "
        f"{alter} Tagen auf Aktualisierungen geprüft.\n"
        f"Zu tun: Veröffentlichungen von htmx ansehen, bei Bedarf "
        f"src/moneten/static/js/htmx.min.js austauschen, die Fassung in der "
        f"Tabelle nachziehen und das Datum in THIRD-PARTY-NOTICES.md neu setzen."
    )
