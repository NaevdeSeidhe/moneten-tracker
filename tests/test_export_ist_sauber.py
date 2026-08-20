"""Der Export-Prüfer läuft bei JEDEM Testlauf mit, nicht erst beim Hochladen.

**Warum das nötig war.** Der Prüfer war ein Schritt im
Veröffentlichungs-Skript. Wer Code schreibt und die Suite laufen lässt, erfuhr
also erst beim nächsten Push, dass etwas Privates in einem Kommentar steht — und
bis dahin lag es im Baum.

Ein Wächter, der nur läuft, wenn man an ihn denkt, schützt genau dann nicht,
wenn man nicht an ihn denkt.

**Warum in einen Temp-Ordner gebaut wird.** Der Test baut den Export neu — sonst
prüfte er einen Stand von gestern und meldete „sauber" für Dateien, die es so
nicht mehr gibt. Er baut aber NICHT in den echten Export-Ordner: dort liegt ein
Git-Repository, und ein Testlauf hat in einem Arbeitsbaum nichts zu ändern.

Läuft nur im Arbeitsordner des Autors — im veröffentlichten Repository gibt es
weder Bauer noch Prüfer, und dort ist die Frage auch schon beantwortet.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]
BAUER = WURZEL / "veroeffentlichen" / "export_bauen.py"
PRUEFER = WURZEL / "veroeffentlichen" / "pruefer.py"

pytestmark = pytest.mark.skipif(
    not (BAUER.is_file() and PRUEFER.is_file()),
    reason="Bauer und Prüfer gehören zum Arbeitsordner des Autors",
)


def _lauf(skript: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(skript), *args],
        cwd=WURZEL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=900,
    )


def test_ein_frisch_gebauter_export_ist_sauber(tmp_path: Path) -> None:
    """Baut den Export in einen Wegwerf-Ordner und lässt den Prüfer darüber."""
    ziel = tmp_path / "export"
    bau = _lauf(BAUER, str(ziel))
    assert bau.returncode == 0, f"Der Export-Bau scheiterte:\n{bau.stdout}\n{bau.stderr}"

    pruefung = _lauf(PRUEFER, str(ziel))
    assert pruefung.returncode == 0, (
        "Der Prüfer hat im Export etwas gefunden. Das gehört NICHT ins "
        "öffentliche Repository:\n\n" + pruefung.stdout[-3000:]
    )


def test_der_pruefer_wuerde_etwas_finden(tmp_path: Path) -> None:
    """Gegenprobe: ein sauberer Lauf ist nur dann eine Aussage, wenn der Prüfer
    an derselben Stelle auch anschlägt.

    Ohne diesen Test bliebe offen, ob oben wirklich geprüft wurde oder ob der
    Prüfer bloss nichts zu lesen fand.
    """
    ziel = tmp_path / "export"
    _lauf(BAUER, str(ziel))

    # Zur Laufzeit zusammengesetzt. Stünde die IBAN als Literal hier, meldete
    # der Prüfer beim nächsten Export diese Testdatei — und hätte damit recht.
    iban = " ".join(["CH93", "0076", "2011", "6238", "5295", "7"])
    (ziel / "src" / "moneten" / "gepflanzt.py").write_text(
        f"# Konto: {iban}" + chr(10), encoding="utf-8",
    )
    pruefung = _lauf(PRUEFER, str(ziel))
    assert pruefung.returncode != 0, "Der Prüfer übersieht eine untergeschobene IBAN"
    assert "IBAN" in pruefung.stdout
