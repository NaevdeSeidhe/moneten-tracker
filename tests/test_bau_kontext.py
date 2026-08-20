"""Was das Dockerfile kopiert, muss in JEDEM Auslieferungsweg liegen.

Es gibt zwei davon, und beide haben ihre eigene Dateiliste:

* ``deploy.ps1`` packt ein tar für das NAS,
* ``veroeffentlichen/export_bauen.py`` baut das öffentliche Repository.

Wer eine Datei hinzufügt und nur an eine der beiden Listen denkt, merkt es erst
beim Bauen — und zwar dort, wo es weh tut. Genau das ist passiert:
``requirements-docker.txt`` stand im Export und fehlte im Deploy-Paket, der Bau
auf dem NAS brach ab mit::

    COPY failed: file not found in build context: requirements-docker.txt

Die Suite kann den Docker-Bau nicht ausführen. Die Frage, ob die Zutaten
beisammen sind, kann sie sehr wohl beantworten.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
DOCKERFILE = WURZEL / "Dockerfile"

#: Die beiden Listen liegen ausserhalb des Pakets — in einem Klon gibt es sie nicht.
DEPLOY = WURZEL.parent / "deploy.ps1"
EXPORT_BAUER = WURZEL.parent / "veroeffentlichen" / "export_bauen.py"


def _kopierte_quellen() -> list[str]:
    """Alle Quellen aus ``COPY``-Zeilen des Dockerfiles, ohne das Ziel."""
    quellen: list[str] = []
    for zeile in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if not zeile.strip().upper().startswith("COPY "):
            continue
        teile = re.split(r"\s+", zeile.strip())[1:]
        teile = [t for t in teile if not t.startswith("--")]
        # Das letzte Feld ist das Ziel im Abbild.
        quellen += [t.rstrip("/") for t in teile[:-1]]
    return quellen


def test_es_gibt_ueberhaupt_copy_zeilen() -> None:
    """Sonst prüfen die beiden Tests unten fröhlich nichts."""
    assert _kopierte_quellen(), "Keine COPY-Zeile im Dockerfile gefunden"


@pytest.mark.skipif(not DEPLOY.is_file(), reason="deploy.ps1 gehört zum Arbeitsordner des Autors")
def test_das_deploy_paket_enthaelt_alles_kopierte() -> None:
    """Der gemessene Fall: Bau bricht auf dem NAS ab, mitten im Deploy."""
    text = DEPLOY.read_text(encoding="utf-8")
    fehlend = [q for q in _kopierte_quellen() if q not in text]
    assert not fehlend, (
        "Das Dockerfile kopiert diese Pfade, deploy.ps1 packt sie nicht ein: "
        f"{', '.join(fehlend)}"
    )


@pytest.mark.skipif(
    not EXPORT_BAUER.is_file(), reason="Der Export-Bauer gehört zum Arbeitsordner des Autors"
)
def test_der_export_enthaelt_alles_kopierte() -> None:
    """Dasselbe für den, der das Repository klont: fehlt eine Datei, scheitert
    sein erster ``docker compose up --build`` — und er weiss nicht, warum."""
    text = EXPORT_BAUER.read_text(encoding="utf-8")
    fehlend = [q for q in _kopierte_quellen() if q not in text]
    assert not fehlend, (
        "Das Dockerfile kopiert diese Pfade, export_bauen.py nimmt sie nicht mit: "
        f"{', '.join(fehlend)}"
    )
