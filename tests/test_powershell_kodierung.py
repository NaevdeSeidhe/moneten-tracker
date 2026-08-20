"""PowerShell-Skripte mit Umlauten brauchen einen BOM — sonst liest 5.1 sie falsch.

**Der Fehler, der das hier ausgelöst hat.** Ein Werkzeug schrieb ``deploy.ps1``
als UTF-8 **ohne** Byte-Order-Mark zurück. Windows PowerShell 5.1 nimmt eine
``.ps1`` ohne BOM aber als ANSI (cp1252) an: aus ``ü`` werden zwei Zeichen, aus
``„`` drei — und sobald in einem davon ein Anführungszeichen steckt, zerfällt der
ganze Rest der Datei. Das Skript brach mit acht Parser-Fehlern ab, keiner davon
an der Stelle, an der wirklich etwas geändert worden war.

Besonders tückisch: ``[Parser]::ParseFile`` liest die Datei als UTF-8 und meldet
**keinen** Fehler. Eine Syntaxprüfung geht also durch, während der echte Aufruf
scheitert. Geprüft werden muss deshalb die Kodierung selbst, nicht die Syntax.

Reines ASCII braucht keinen BOM — dort gibt es nichts misszuverstehen.

**In einem Klon dieses Repositorys wird übersprungen.** Die Skripte gehören zum
Arbeitsordner des Autors und werden nicht mitgeliefert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]
BOM = b"\xef\xbb\xbf"


def _skripte() -> list[Path]:
    return sorted(p for p in WURZEL.glob("*.ps1") if p.is_file())


@pytest.mark.skipif(not _skripte(), reason="PowerShell-Skripte gehören zum Arbeitsordner des Autors")
def test_skripte_mit_umlauten_tragen_einen_bom() -> None:
    ohne = []
    for skript in _skripte():
        roh = skript.read_bytes()
        if roh.startswith(BOM):
            continue
        if any(b > 127 for b in roh):
            ohne.append(skript.name)
    assert not ohne, (
        "Diese Skripte enthalten Nicht-ASCII-Zeichen ohne BOM. PowerShell 5.1 liest sie "
        f"als cp1252 und zerlegt dabei die Datei: {ohne}"
    )


@pytest.mark.skipif(not _skripte(), reason="PowerShell-Skripte gehören zum Arbeitsordner des Autors")
def test_die_skripte_sind_gueltiges_utf8() -> None:
    """Ein BOM vor kaputten Bytes wäre nur eine hübschere Lüge."""
    kaputt = []
    for skript in _skripte():
        roh = skript.read_bytes()
        try:
            roh.removeprefix(BOM).decode("utf-8")
        except UnicodeDecodeError as fehler:
            kaputt.append(f"{skript.name}: {fehler}")
    assert not kaputt, kaputt
