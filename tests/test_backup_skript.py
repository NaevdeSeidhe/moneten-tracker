"""Das Python IM Backup-Skript sieht kein Werkzeug an.

``backup.sh`` schickt ein Stück Python per ``docker exec … python -c "…"`` in den
Container. Für ruff ist das eine Zeichenkette, für die Suite bis jetzt gar nichts
— geprüft wurde es zum ersten Mal auf dem NAS, mitten im Deploy, im Schritt, der
das Sicherheitsnetz vor den Migrationen spannt. Zwei Fehler sind dort
nacheinander aufgetreten:

1. CRLF-Zeilenenden liessen die Shell an ``set -euo pipefail`` scheitern
   (siehe ``test_dateien_sind_sauber.py``).
2. ``from moneten.db.session import engine`` — der ALTE, noch laufende Container
   kennt das Paket unter seinem früheren Namen. ``ModuleNotFoundError``, Deploy
   abgebrochen.

Dieser Test liest den eingebetteten Block aus dem Skript und prüft ihn, so wie
jede andere Python-Datei auch geprüft wird.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
SKRIPT = WURZEL / "scripts" / "backup.sh"

#: Der Block zwischen ``python -c "`` und dem schliessenden ``"`` am Zeilenanfang.
_EINGEBETTET = re.compile(r'python -c "\n(.*?)\n"\n', re.S)


def _bloecke() -> list[str]:
    text = SKRIPT.read_text(encoding="utf-8")
    # In der Shell steht ``\"`` für ein Anführungszeichen — für Python ist es eines.
    return [b.replace('\\"', '"') for b in _EINGEBETTET.findall(text)]


def test_es_gibt_ueberhaupt_einen_eingebetteten_block() -> None:
    """Sonst prüfte dieser Test fröhlich nichts."""
    assert _bloecke(), (
        "Kein `python -c` in backup.sh gefunden — entweder ist es weg (dann kann "
        "dieser Test weg) oder das Muster passt nicht mehr."
    )


@pytest.mark.parametrize("nr", range(len(_bloecke()) or 1))
def test_der_eingebettete_block_ist_gueltiges_python(nr: int) -> None:
    """Ein Tippfehler darin fällt sonst erst im Deploy auf."""
    bloecke = _bloecke()
    if not bloecke:
        pytest.skip("kein Block vorhanden — der andere Test meldet das")
    ast.parse(bloecke[nr])


def test_der_snapshot_kennt_beide_paketnamen() -> None:
    """Das Skript kommt aus dem NEUEN Baum und läuft im ALTEN Container.

    Beim Deploy, der die Umbenennung ausrollt, ist genau das der Fall: gesichert
    wird mit dem neuen Skript, ausgeführt im alten Abbild. Ohne den Rückfall
    scheitert das Backup — und damit der ganze Deploy, absichtlich, denn ohne
    frische Sicherung soll keine Migration laufen.
    """
    quelle = "\n".join(_bloecke())
    assert "moneten.db.session" in quelle, "Der neue Paketname fehlt"
    assert "bilanz.db.session" in quelle, (
        "Kein Rückfall auf den alten Paketnamen — der Deploy über die Umbenennung "
        "hinweg scheitert dann im Backup."
    )
    assert "ModuleNotFoundError" in quelle, (
        "Der Rückfall fängt zu breit (oder gar nicht) — er soll genau den "
        "fehlenden Paketnamen abfangen, nichts anderes."
    )


# ---------------------------------------------------------------------------
# Das ganze Skript einmal durchlaufen lassen
# ---------------------------------------------------------------------------
def _stub(ordner: Path, name: str, rumpf: str) -> None:
    p = ordner / name
    p.write_text("#!/usr/bin/env bash\n" + rumpf + "\n", encoding="utf-8")
    p.chmod(0o755)


@pytest.mark.skipif(shutil.which("bash") is None, reason="Keine bash-Shell vorhanden")
def test_das_skript_laeuft_von_vorne_bis_hinten_durch(tmp_path) -> None:
    """Alles nach der Snapshot-Zeile hat noch nie jemand ausgeführt.

    Zweimal ist der Deploy in diesem Skript gescheitert, beide Male an einer
    Stelle, die kein Werkzeug ansieht. Der Rest — Verschieben des Snapshots,
    Spiegeln der Belege, Aufräumen alter Sicherungen — war nie geprüft. Hier
    läuft er mit gestelltem ``docker`` und ``rsync`` einmal komplett.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    daten = tmp_path / "data"
    (daten / "attachments").mkdir(parents=True)
    (daten / "attachments" / "beleg_erfunden.pdf").write_bytes(b"%PDF-1.4 erfunden")
    ziel = tmp_path / "backups"

    # ``docker exec … python -c`` legt den Snapshot an, wie es der echte Aufruf
    # täte; alles andere ist folgenlos.
    _stub(stubs, "docker", f'''
if [ "$1" = "exec" ] && [ "$3" = "python" ]; then
  printf 'schnappschuss' > "{(daten / ".snapshot.db").as_posix()}"
  echo "snapshot ok"
  exit 0
fi
exit 0''')
    _stub(stubs, "rsync", 'cp -r "${@: -2:1}"* "${@: -1}" 2>/dev/null; exit 0')

    umgebung = {
        **os.environ,
        "PATH": f"{stubs.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "MONETEN_CONTAINER": "bilanz",
        "MONETEN_HOST_DATA": daten.as_posix(),
    }
    ergebnis = subprocess.run(
        [shutil.which("bash"), str(SKRIPT), ziel.as_posix()],
        env=umgebung, capture_output=True, text=True, timeout=120,
        # Ohne feste Kodierung liest Python die Ausgabe in der Windows-Codepage,
        # und aus jedem Umlaut wird Buchstabensalat — der Test verglich dann
        # gegen etwas, das nie kommen kann.
        encoding='utf-8',
    )
    assert ergebnis.returncode == 0, f"{ergebnis.stdout}\n{ergebnis.stderr}"

    gesichert = list((ziel / "db").glob("*.db"))
    assert len(gesichert) == 1, f"{len(gesichert)} Sicherungen: {gesichert}"
    assert gesichert[0].read_bytes() == b"schnappschuss"
    assert not (daten / ".snapshot.db").exists(), "Der Snapshot blieb im Datenordner liegen"
    assert "Off-Site übersprungen" in ergebnis.stdout
    assert "[backup] OK" in ergebnis.stdout
