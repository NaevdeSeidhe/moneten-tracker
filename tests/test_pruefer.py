"""Der Prüfer ist die letzte Instanz vor dem Hochladen — und war ungeprüft.

``veroeffentlichen/pruefer.py`` entscheidet, ob ein Export echte Kontodaten
enthält. Fällt er still aus, merkt es niemand: er meldet dann „Sauber", und
genau das wollte man ja lesen. Ein Wächter ohne eigenen Wächter ist eine
Behauptung.

Gemessen und deshalb hier festgehalten: die Ausnahmeliste galt früher für die
ganze **Zeile**. Ein echter Betrag auf einer Zeile, in der irgendwo „muster"
oder ``127.0.0.1`` stand, wäre durchgegangen — der Abgleich gegen die von Hand
gelesenen Werte lief nach dem Übersprungen-Werden gar nicht mehr. Die Tests
unten halten beides fest: dass Ausnahmen nur ihr eigenes Muster decken, und dass
der Abgleich gegen echte Werte von keiner Ausnahme erreichbar ist.

Der Prüfer gehört zum Arbeitsordner des Autors und liegt nicht im Export — in
einem Klon überspringt sich diese Datei.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PRUEFER = Path(__file__).resolve().parents[2] / "veroeffentlichen" / "pruefer.py"
pytestmark = pytest.mark.skipif(
    not _PRUEFER.is_file(), reason="Der Prüfer gehört zum Arbeitsordner des Autors"
)


@pytest.fixture(scope="module")
def P():
    """Den Prüfer als Modul laden, ohne ihn irgendwo zu installieren."""
    spec = importlib.util.spec_from_file_location("pruefer_unter_test", _PRUEFER)
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _ordner_mit(tmp_path: Path, name: str, inhalt: str) -> Path:
    ordner = tmp_path / "probe"
    ordner.mkdir(exist_ok=True)
    (ordner / name).write_text(inhalt, encoding="utf-8")
    return ordner


# ---------------------------------------------------------------------------
# Die Ausnahmen
# ---------------------------------------------------------------------------
def test_jede_ausnahme_nennt_bekannte_muster(P) -> None:
    """Ein Tippfehler im Musternamen macht die Ausnahme still wirkungslos.

    Das ist die harmlose Richtung — gemeldet wird dann zu viel. Der Test steht
    trotzdem hier: eine Ausnahme, die nicht tut, was ihre Begründung behauptet,
    führt beim nächsten Lesen in die Irre.
    """
    bekannt = set(P.MUSTER) | {"moegliches Geheimnis"}
    for _m, grund, fuer in P.ERLAUBT:
        assert fuer, f"Ausnahme ohne Muster: {grund}"
        unbekannt = set(fuer) - bekannt
        assert not unbekannt, f"{grund}: unbekannte Musternamen {unbekannt}"


def test_keine_ausnahme_deckt_die_ganze_zeile(P) -> None:
    """Die Bauart, die den Fehler ermöglicht hat, soll nicht zurückkommen."""
    for _m, grund, fuer in P.ERLAUBT:
        assert isinstance(fuer, tuple) and fuer, (
            f"{grund}: eine Ausnahme muss nennen, wogegen sie gilt. "
            "Eine, die für alles galt, hat schon einmal einen echten Wert gedeckt."
        )


def test_eine_ausnahme_deckt_nur_ihr_eigenes_muster(P, tmp_path) -> None:
    """Der gemessene Fehler: Platzhalter-Zeile mit einer IBAN darauf.

    „muster" stellt Adresse, Name, Telefon und Betrag frei — die IBAN nicht.
    Vorher hätte das Wort die ganze Zeile freigestellt.
    """
    ordner = _ordner_mit(
        tmp_path, "probe.py",
        f"# Musterstrasse 1, 8000 Musterstadt — Konto {_ERFUNDENE_IBAN}" + chr(10),
    )
    befunde = P.pruefe(ordner)
    assert any("IBAN" in b for b in befunde), befunde
    assert not any("Adresse mit PLZ" in b for b in befunde), befunde


def test_der_abgleich_gegen_echte_werte_kennt_keine_ausnahme(P, tmp_path, monkeypatch) -> None:
    """Der wichtigste Test dieser Datei.

    Die von Hand aus den Belegen gelesenen Werte liegen ausserhalb des Repos.
    Taucht einer davon im Export auf, ist das ein Befund — unabhängig davon, was
    sonst noch auf der Zeile steht. Hier steht bewusst ein Wort auf derselben
    Zeile, das mehrere Muster freistellt.
    """
    monkeypatch.setattr(P, "_handgelesene_werte", lambda: {_ERFUNDENER_BETRAG})
    ordner = _ordner_mit(tmp_path, "probe.py", f"# muster localhost {_ERFUNDENER_BETRAG}" + chr(10))
    befunde = P.pruefe(ordner)
    assert any("HANDGELESENER WERT" in b for b in befunde), befunde


#: Ein erfundener Schlüssel — **zur Laufzeit zusammengesetzt**.
#:
#: Stünde er als Literal in dieser Datei, meldete der Prüfer beim nächsten
#: Export seine eigene Testdatei. Das ist keine Marotte: die Alternative wäre
#: eine Ausnahme für genau diese Datei gewesen — und die hätte ab dann auch
#: jeden echten Schlüssel gedeckt, den hier je jemand hineinkopiert. Die
#: Teilstücke sehen einzeln wie Bezeichner aus und fallen deshalb durch;
#: zusammengefügt trägt die Kette ein Pluszeichen und schlägt an.
_ERFUNDENER_SCHLUESSEL = "+".join(["Qw8xR2vL7pT4mK9n", "Z5bY3cF6hJ1sD0gA", "8eU2iO4wQ7rE"])

#: Dieselbe Ueberlegung, dieselbe Bauart: eine IBAN und ein Betrag, die wie
#: echte aussehen sollen — sonst pruefen die Tests unten nichts Ernsthaftes.
_ERFUNDENE_IBAN = " ".join(["CH93", "0076", "2011", "6238", "5295", "7"])
_ERFUNDENER_BETRAG = "9'876" + ".54"


def test_ein_geheimnis_wird_gemeldet(P, tmp_path) -> None:
    """Gegenprobe zur Ausnahme für den Entwicklungsschlüssel: ein anderer
    Schlüssel derselben Bauart muss sehr wohl auffallen."""
    ordner = _ordner_mit(tmp_path, "probe.py", f'SCHLUESSEL = "{_ERFUNDENER_SCHLUESSEL}"' + chr(10))
    assert any("Geheimnis" in b for b in P.pruefe(ordner)), P.pruefe(ordner)
# ---------------------------------------------------------------------------
# Die groben Sperren
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["data", "_backups", ".claude"])
def test_verbotene_ordner_werden_gemeldet(P, tmp_path, name: str) -> None:
    ordner = tmp_path / "probe"
    (ordner / name).mkdir(parents=True)
    (ordner / name / "egal.txt").write_text("nichts", encoding="utf-8")
    assert any("VERBOTENER ORDNER" in b for b in P.pruefe(ordner)), P.pruefe(ordner)


@pytest.mark.parametrize("name", [".env", "verlaeufe_manuell.json", "deploy.ps1"])
def test_verbotene_dateien_werden_gemeldet(P, tmp_path, name: str) -> None:
    ordner = _ordner_mit(tmp_path, name, "egal\n")
    assert any("VERBOTENE DATEI" in b for b in P.pruefe(ordner)), P.pruefe(ordner)


def test_eine_datenbank_faellt_ueber_die_endung_auf(P, tmp_path) -> None:
    ordner = _ordner_mit(tmp_path, "moneten.db", "SQLite format 3\n")
    assert any("VERBOTENE ENDUNG" in b for b in P.pruefe(ordner)), P.pruefe(ordner)


def test_ein_sauberer_ordner_ist_sauber(P, tmp_path, monkeypatch) -> None:
    """Sonst wäre der Prüfer nur laut, nicht scharf."""
    monkeypatch.setattr(P, "_handgelesene_werte", lambda: set())
    ordner = _ordner_mit(tmp_path, "probe.py", "def hallo() -> str:\n    return 'welt'\n")
    assert P.pruefe(ordner) == []
