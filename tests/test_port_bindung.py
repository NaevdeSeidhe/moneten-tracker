"""Der Port des Containers gehört dem Host — sonst niemandem.

**Was hier eigentlich geprüft wird.** Eine einzelne Zeile in
``docker-compose.yml`` entscheidet, ob die Anmeldeseite im ganzen Heimnetz
erreichbar ist. Sie lässt sich beim Fehlersuchen in fünf Sekunden aufmachen
("nur kurz mal von hier aus schauen") und bleibt dann offen, weil nichts daran
erinnert. Dieser Test ist die Erinnerung.

**Und warum es mehr ist als eine Vorliebe.** Docker maskiert jede Verbindung
hinter dem Brücken-Gateway: die App sieht bei ALLEN Anfragen dieselbe Adresse.
Die Login-Drossel zählt deshalb an ``X-Forwarded-For`` — einem Wert, den der
Klopfende selbst setzt, sobald er den Port erreicht. Mit der engen Bindung
erreicht ihn nur der Host, also der Reverse-Proxy und Tailscale, und der
weitergereichte Wert stimmt wieder. Fällt die Bindung, fällt die Drossel mit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WURZEL = Path(__file__).resolve().parents[1]

#: Die eigene Anlage und die Vorlage im veröffentlichten Repository.
DATEIEN = [
    WURZEL / "docker-compose.yml",
    WURZEL.parent / "veroeffentlichen" / "vorlagen" / "docker-compose.yml",
]

#: ``127.0.0.1:8000:8000`` — Host-Adresse, Host-Port, Container-Port.
#:
#: Jede Adresse aus 127.x zaehlt, nicht nur die 1 — eine andere Loopback-Adresse
#: waere ein denkbarer Weg, einen Weiterleitungsdienst auszusperren, der nur
#: ``127.0.0.1`` kennt. Was zaehlt, ist allein: der Port darf nicht auf allen
#: Schnittstellen haengen.
_ENG = re.compile(r"^(127\.\d{1,3}\.\d{1,3}\.\d{1,3}|localhost):\d+:\d+$")


def _portzeilen(pfad: Path) -> list[str]:
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    zeilen: list[str] = []
    for dienst in (daten.get("services") or {}).values():
        for eintrag in dienst.get("ports") or []:
            # Lange Schreibweise: {target: 8000, published: 8000, ...}
            if isinstance(eintrag, dict):
                zeilen.append(f"{eintrag.get('host_ip', '')}:{eintrag.get('published')}:{eintrag.get('target')}")
            else:
                zeilen.append(str(eintrag))
    return zeilen


@pytest.mark.parametrize("pfad", DATEIEN, ids=lambda p: p.parent.name)
def test_der_port_haengt_nur_am_host(pfad: Path) -> None:
    if not pfad.is_file():
        pytest.skip(f"{pfad.name} gehört zum Arbeitsordner des Autors")

    zeilen = _portzeilen(pfad)
    assert zeilen, f"{pfad}: keine ports-Angabe gefunden — prüft dieser Test noch etwas?"

    offen = [z for z in zeilen if not _ENG.match(z)]
    assert not offen, (
        f"{pfad}: diese Ports liegen auf allen Schnittstellen: {offen}. "
        "Damit erreicht jedes Gerät im Netz die Anmeldeseite, und die "
        'Login-Drossel ist über einen selbst gesetzten "X-Forwarded-For" zu '
        'umgehen. Gewollt ist "127.0.0.1:8000:8000".'
    )
