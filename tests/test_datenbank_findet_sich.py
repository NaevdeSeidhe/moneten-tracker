"""Die Umbenennung darf niemanden von seinen Daten trennen.

**Der Beinahe-Unfall.** Beim Umbenennen des Programms wanderte auch der
Standardname der Datenbankdatei mit — von ``bilanz.db`` auf ``moneten.db``. Wer
den Pfad in seiner ``.env`` stehen hat, merkt davon nichts. Wer ihn nicht stehen
hat, bekäme beim nächsten Start eine **frische, leere** Datenbank neben der
alten: die App läuft, die Anmeldung geht, und alles ist scheinbar weg. Kein
Fehler, keine Meldung.

Gefunden wurde es nicht durch Nachdenken, sondern beim Blick auf die
Verläufe-Seite nach dem Umbau — dort fehlte eine Reihe, und die Spur führte zum
Dateinamen.

Der Rückfall gilt **nur für den Standard**. Wer einen Pfad ausdrücklich setzt,
bekommt genau den; eine Vermutung darf keine Angabe überstimmen.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from moneten.config import Settings

STANDARD = "sqlite:///./data/moneten.db"


@pytest.fixture()
def im_leeren_ordner(tmp_path, monkeypatch):
    """Arbeitet in einem leeren Ordner — die Prüfung schaut auf relative Pfade."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    for name in list(os.environ):
        if name.startswith(("MONETEN_", "BILANZ_")):
            monkeypatch.delenv(name, raising=False)
    return tmp_path


def _url(ordner: Path) -> str:
    return Settings(_env_file=str(ordner / "fehlt.env")).database_url


def test_ohne_beide_dateien_bleibt_es_beim_standard(im_leeren_ordner: Path) -> None:
    """Eine frische Installation legt die Datei unter dem neuen Namen an."""
    assert _url(im_leeren_ordner) == STANDARD


def test_alte_datei_wird_weiterbenutzt(im_leeren_ordner: Path) -> None:
    """Der eigentliche Zweck: bestehende Daten bleiben erreichbar."""
    (im_leeren_ordner / "data/bilanz.db").write_bytes(b"")
    assert _url(im_leeren_ordner) == "sqlite:///./data/bilanz.db"


def test_neue_datei_hat_vorrang(im_leeren_ordner: Path) -> None:
    """Liegen beide da, gilt die neue — sonst bliebe man ewig am alten Namen."""
    (im_leeren_ordner / "data/bilanz.db").write_bytes(b"")
    (im_leeren_ordner / "data/moneten.db").write_bytes(b"")
    assert _url(im_leeren_ordner) == STANDARD


def test_eine_ausdrueckliche_angabe_wird_nicht_ueberstimmt(
    im_leeren_ordner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wer den Pfad setzt, bekommt ihn — auch wenn daneben eine alte Datei liegt.

    Ohne diese Grenze würde die Vermutung eine bewusste Entscheidung
    überschreiben, und das wäre schlimmer als das Problem, das sie löst.
    """
    (im_leeren_ordner / "data/bilanz.db").write_bytes(b"")
    monkeypatch.setenv("MONETEN_DATABASE_URL", "sqlite:///./data/eigene.db")
    assert _url(im_leeren_ordner) == "sqlite:///./data/eigene.db"
