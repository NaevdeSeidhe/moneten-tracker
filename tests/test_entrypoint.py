"""Der Entrypoint gibt Privilegien ab — und darf dabei niemanden aussperren.

**Warum das getestet wird und nicht bloss gelesen.** Dieses Skript ist die
einzige Stelle im Projekt, die eine laufende Installation unerreichbar machen
kann: Es läuft vor dem Server, `set -euo pipefail` bricht bei jedem Fehler ab,
und `restart: unless-stopped` macht daraus eine Neustartschleife. Wer das falsch
baut, merkt es erst, wenn die App nach einem Deploy nicht mehr hochkommt — auf
dem NAS, ohne Browser, abends.

Docker gibt es in der Testumgebung nicht. Geprüft wird deshalb das Skript selbst,
mit **gestellten Werkzeugen**: ``id``, ``find``, ``chown``, ``setpriv``,
``alembic`` und ``uvicorn`` liegen als kleine Skripte in einem Temp-Ordner, der
im PATH ganz vorne steht. Damit lässt sich jeder Zweig durchspielen, ohne etwas
anzufassen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
SKRIPT = WURZEL / "scripts" / "entrypoint.sh"

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="Keine bash-Shell vorhanden")


def _stub(ordner: Path, name: str, rumpf: str) -> None:
    """Legt ein ausführbares Mini-Skript im Stub-Ordner an."""
    pfad = ordner / name
    pfad.write_text("#!/usr/bin/env bash\n" + rumpf + "\n", encoding="utf-8")
    pfad.chmod(0o755)


def _lauf(
    tmp_path: Path,
    *,
    uid: str = "0",
    fremde_datei: bool = True,
    chown_geht: bool = True,
    umgebung: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Führt den Entrypoint mit gestellten Werkzeugen aus.

    ``uid`` ist, was ``id -u`` meldet; ``fremde_datei`` steuert, ob ``find`` etwas
    findet (also ob die Besitzrechte nicht stimmen).
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    protokoll = tmp_path / "aufrufe.txt"

    _stub(stubs, "id", f'echo "${{MONETEN_TEST_UID:-{uid}}}"')
    _stub(stubs, "find", 'echo "/app/data/moneten.db"' if fremde_datei else "true")
    _stub(stubs, "chown", f'echo "chown $*" >> "{protokoll.as_posix()}"; '
                          + ("true" if chown_geht else "exit 1"))
    # ``setpriv`` startet das Skript WIRKLICH neu — mit gewechselter Kennung.
    #
    # Vorher brach der Stub hier ab und prüfte nur, WIE aufgerufen wurde. Genau
    # dadurch blieb der Fehler unentdeckt, der auf dem NAS die Neustartschleife
    # auslöste: ``setpriv … "$0"`` führt die Datei als Programm aus und braucht
    # dafür das Ausführbar-Bit, das ein Windows-tar nicht mitbringt. Der Stub
    # legt die letzten Argumente jetzt selbst aus — steht dort ``bash``, läuft
    # der zweite Durchgang; stünde dort die nackte Datei, wäre der Test so
    # aussagekräftig wie vorher, aber der Aufruf ist nachprüfbar.
    _stub(stubs, "setpriv", f'''
echo "setpriv $*" >> "{protokoll.as_posix()}"
befehl=""
for a in "$@"; do case "$a" in --*) ;; *) befehl="$befehl $a";; esac; done
MONETEN_TEST_UID=10001 exec $befehl''')
    _stub(stubs, "alembic", f'echo "alembic $*" >> "{protokoll.as_posix()}"')
    _stub(stubs, "uvicorn", f'echo "uvicorn $*" >> "{protokoll.as_posix()}"')

    env = dict(os.environ)
    env["PATH"] = f"{stubs.as_posix()}{os.pathsep}{env['PATH']}"
    env["MONETEN_DATA_DIR"] = (tmp_path / "daten").as_posix()
    env.update(umgebung or {})

    ergebnis = subprocess.run(
        [_BASH, str(SKRIPT)], env=env, capture_output=True, text=True, timeout=60,
    )
    ergebnis.protokoll = protokoll.read_text(encoding="utf-8") if protokoll.exists() else ""  # type: ignore[attr-defined]
    return ergebnis


def test_skript_ist_syntaktisch_in_ordnung() -> None:
    """Ein Tippfehler hier kostet einen Deploy."""
    probe = subprocess.run([_BASH, "-n", str(SKRIPT)], capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr


def test_als_root_werden_rechte_gesetzt_und_privilegien_abgegeben(tmp_path) -> None:
    """Der Normalfall auf einer bestehenden Anlage: alles gehört noch root."""
    e = _lauf(tmp_path, uid="0", fremde_datei=True)
    assert e.returncode == 0, e.stderr
    assert "chown -R 10001:10001" in e.protokoll, e.protokoll
    assert "setpriv --reuid=10001 --regid=10001 --clear-groups" in e.protokoll, e.protokoll
    # Der Neustart geht über ``bash``, nicht über die Datei selbst: sonst
    # entscheidet das Ausführbar-Bit, und aus einem Windows-tar kommt keines.
    assert "clear-groups bash" in e.protokoll, e.protokoll
    # Und der zweite Durchgang läuft wirklich durch — bis zum Server.
    assert "alembic upgrade head" in e.protokoll, e.protokoll
    assert "uvicorn moneten.main:app" in e.protokoll, e.protokoll


def test_ohne_fremde_dateien_wird_nicht_durchgechownt(tmp_path) -> None:
    """Ein ``chown -R`` über tausende Belege bei jedem Start wäre Verschwendung."""
    e = _lauf(tmp_path, uid="0", fremde_datei=False)
    assert e.returncode == 0, e.stderr
    assert "chown" not in e.protokoll, e.protokoll
    assert "setpriv" in e.protokoll


def test_gescheitertes_chown_sagt_was_zu_tun_ist(tmp_path) -> None:
    """Auf einer Freigabe mit erzwungenem Eigentümer geht es nicht — dann bricht
    der Start ab, aber mit einem Satz, der weiterhilft.

    Der Fehler DARF hier hart sein: liefe die App weiter, käme drei Zeilen später
    ein „attempt to write a readonly database", und niemand fände den Grund.
    """
    e = _lauf(tmp_path, uid="0", fremde_datei=True, chown_geht=False)
    assert e.returncode == 1
    assert "MONETEN_UID=0" in e.stderr, e.stderr
    assert "setpriv" not in e.protokoll


def test_notausgang_laesst_alles_als_root_laufen(tmp_path) -> None:
    """``MONETEN_UID=0`` ist der Weg zurück, wenn beim Deploy etwas klemmt."""
    e = _lauf(tmp_path, uid="0", umgebung={"MONETEN_UID": "0"})
    assert e.returncode == 0, e.stderr
    assert "setpriv" not in e.protokoll
    assert "chown" not in e.protokoll
    assert "alembic upgrade head" in e.protokoll
    assert "uvicorn moneten.main:app" in e.protokoll


def test_als_unprivilegierter_benutzer_wird_direkt_gestartet(tmp_path) -> None:
    """Der zweite Durchlauf nach ``setpriv``: nicht noch einmal abgeben wollen."""
    e = _lauf(tmp_path, uid="10001")
    assert e.returncode == 0, e.stderr
    assert "setpriv" not in e.protokoll
    assert "chown" not in e.protokoll
    assert "alembic upgrade head" in e.protokoll
    assert "uvicorn moneten.main:app" in e.protokoll


def test_eigene_uid_wird_uebernommen(tmp_path) -> None:
    """Wer die Dateien seinem SSH-Benutzer geben will, setzt UID/GID selbst."""
    e = _lauf(tmp_path, uid="0", umgebung={"MONETEN_UID": "1026", "MONETEN_GID": "100"})
    assert e.returncode == 0, e.stderr
    assert "chown -R 1026:100" in e.protokoll, e.protokoll
    assert "setpriv --reuid=1026 --regid=100" in e.protokoll, e.protokoll


def test_das_dockerfile_setzt_kein_user(tmp_path) -> None:
    """``USER`` im Image würde genau die Neustartschleife erzeugen, die dieses
    Skript verhindert: der Container käme gar nicht erst dazu, die Rechte zu
    richten."""
    dockerfile = (WURZEL / "Dockerfile").read_text(encoding="utf-8")
    zeilen = [z for z in dockerfile.splitlines() if z.strip().startswith("USER ")]
    assert not zeilen, f"USER-Zeile im Dockerfile: {zeilen}"
    assert "useradd" in dockerfile, "Der unprivilegierte Benutzer fehlt im Image"


def test_ohne_setpriv_laeuft_die_app_als_root_weiter(tmp_path) -> None:
    """Eine Härtung darf nicht der Grund sein, warum die App nicht hochkommt.

    Fehlte ``setpriv`` im Abbild, würde ein Abbruch hier zur Neustartschleife —
    also genau zu dem Schaden, den dieses Skript verhindern soll. Als root zu
    laufen ist der Zustand von vorher: nicht besser, aber auch nicht schlimmer.
    """
    stubs = tmp_path / "stubs"
    e = _lauf(tmp_path, uid="0", fremde_datei=True)
    assert e.returncode == 0
    (stubs / "setpriv").unlink()  # ab jetzt gibt es das Werkzeug nicht mehr

    zweiter = subprocess.run(
        [_BASH, str(SKRIPT)],
        env={**os.environ,
             "PATH": f"{stubs.as_posix()}{os.pathsep}{os.environ['PATH']}",
             "MONETEN_DATA_DIR": (tmp_path / "daten").as_posix()},
        capture_output=True, text=True, timeout=60,
    )
    assert zweiter.returncode == 0, zweiter.stderr
    assert "setpriv fehlt" in zweiter.stderr, zweiter.stderr
    assert "alembic upgrade head" in (tmp_path / "aufrufe.txt").read_text(encoding="utf-8")
