"""Was passiert, wenn jemand die App zum ersten Mal aufsetzt — und beim Update.

Zwei Fragen entscheiden, ob man diese App überhaupt weitergeben kann:

1. **Kommt eine frische Anlage hoch?** Leere Datenbank, Migrationskette, Seeds,
   Anmeldeseite. Die übrige Suite baut das Schema mit ``create_all`` und legt
   die Seeds in einer Sitzung an — das ist schnell, aber es ist nicht der Weg,
   den eine echte Installation nimmt.

2. **Überlebt ein fremder Bestand das Update?** Bei jedem Start laufen die Seeds
   erneut. Wer seine Kategorien umbenannt, gelöscht oder ergänzt hat, darf davon
   nichts verlieren — und schon gar nichts doppelt bekommen. Genau das war
   einmal kaputt (zwei Töpfe für dieselbe Sache, siehe ``test_fremder_nutzer.py``);
   dort steht die einzelne Regel, hier der ganze Vorgang.

**Warum als Unterprozess.** Datenbank-Engine und Einstellungen entstehen beim
Import des Moduls. Ein zweiter „Start" im selben Prozess wäre keiner — er würde
die bereits offene Verbindung weiterbenutzen und die Frage gar nicht stellen.
Jeder Start hier ist deshalb ein eigener Prozess mit eigener Umgebung, so wie im
Container.

Alle Daten in dieser Datei sind erfunden.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]


def _umgebung(url: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MONETEN_DB_KEY", None)          # reines SQLite, kein SQLCipher
    env["MONETEN_DATABASE_URL"] = url
    env.setdefault("MONETEN_SECRET_KEY", "test-secret-key")
    env.setdefault("MONETEN_INITIAL_PIN", "424242")
    env["MONETEN_DEV_MODE"] = "true"          # ohne TLS im Test
    return env


def _alembic(*args: str, url: str) -> None:
    p = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=WURZEL, env=_umgebung(url), capture_output=True, text=True, timeout=300,
    )
    assert p.returncode == 0, f"alembic {' '.join(args)}:\n{p.stdout}\n{p.stderr}"


def _start(url: str, code: str) -> dict:
    """Startet die App wie im Container und führt ``code`` danach aus.

    Der Rumpf bekommt ``client`` (die laufende App) und ``db`` (eine Sitzung);
    was er in ``ergebnis`` legt, kommt hier als Wörterbuch zurück.
    """
    kopf = textwrap.dedent(
        """
        import json
        from fastapi.testclient import TestClient

        from moneten.main import app
        from moneten.db.session import SessionLocal

        with TestClient(app) as client:   # lifespan = Seeds, wie beim echten Start
            with SessionLocal() as db:
                ergebnis = {}
        """
    )
    fuss = '\nprint("###" + json.dumps(ergebnis, default=str))\n'
    # Acht Spalten, nicht sechzehn: ``dedent`` hat dem Kopf oben seine
    # Funktionseinrückung genommen, ``ergebnis = {}`` steht danach auf acht.
    rumpf = kopf + textwrap.indent(textwrap.dedent(code), " " * 8) + fuss

    p = subprocess.run(
        [sys.executable, "-c", rumpf],
        cwd=WURZEL, env=_umgebung(url), capture_output=True, text=True, timeout=300,
    )
    assert p.returncode == 0, f"Start schlug fehl:\n{p.stdout}\n{p.stderr}"
    zeile = next((z for z in p.stdout.splitlines() if z.startswith("###")), None)
    assert zeile, f"Kein Ergebnis im Ausgabestrom:\n{p.stdout}\n{p.stderr}"
    return json.loads(zeile[3:])


_ZAEHLEN = """
from sqlalchemy import func, select
from moneten.db.models import Account, Category, MetricSeries, Transaction

antwort = client.get("/login")
ergebnis["login"] = antwort.status_code
ergebnis["konten"] = db.scalar(select(func.count()).select_from(Account))
ergebnis["kategorien"] = db.scalar(select(func.count()).select_from(Category))
ergebnis["reihen"] = db.scalar(select(func.count()).select_from(MetricSeries))
ergebnis["buchungen"] = db.scalar(select(func.count()).select_from(Transaction))
"""


@pytest.fixture()
def frische_anlage(tmp_path) -> str:
    """Leere Datenbank, Migrationskette bis zum Kopf — der Weg des Entrypoints."""
    url = f"sqlite:///{(tmp_path / 'neu.db').as_posix()}"
    _alembic("upgrade", "head", url=url)
    return url


# ---------------------------------------------------------------------------
# 1. Erstinstallation
# ---------------------------------------------------------------------------
def test_eine_frische_anlage_kommt_hoch_und_ist_leer(frische_anlage: str) -> None:
    """Der erste Eindruck eines fremden Benutzers: Anmeldeseite, keine Fremddaten."""
    e = _start(frische_anlage, _ZAEHLEN)

    assert e["login"] == 200, "Die Anmeldeseite kam nicht"
    assert e["konten"] > 0, "Ohne Konto kann man nichts buchen"
    assert e["kategorien"] > 0, "Der Kategorienbaum fehlt"
    assert e["buchungen"] == 0, (
        f"{e['buchungen']} Buchungen in einer FRISCHEN Anlage — es sind fremde "
        "Daten mitgekommen."
    )


def test_der_zweite_start_legt_nichts_doppelt_an(frische_anlage: str) -> None:
    """Jedes Update ist ein zweiter Start. Er darf nichts verdoppeln."""
    erst = _start(frische_anlage, _ZAEHLEN)
    zweit = _start(frische_anlage, _ZAEHLEN)

    for feld in ("konten", "kategorien", "reihen"):
        assert erst[feld] == zweit[feld], (
            f"{feld}: {erst[feld]} -> {zweit[feld]} beim zweiten Start"
        )


# ---------------------------------------------------------------------------
# 2. Update auf einem Bestand, der nicht der eigene ist
# ---------------------------------------------------------------------------
_SEINE_EINRICHTUNG = """
from datetime import date
from decimal import Decimal
from sqlalchemy import select
from moneten.db.models import Account, AccountType, Category, Transaction

# So sieht ein Bestand aus, den jemand anderes bewirtschaftet hat.
eigen = Account(name="Sparkonto Ferien", type=AccountType.SAVINGS)
db.add(eigen)
db.flush()

umbenannt = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
ergebnis["umbenannt_von"] = umbenannt.name
umbenannt.name = "Meine eigene Gruppe"

weg = db.scalars(select(Category).where(Category.parent_id.is_not(None))).first()
ergebnis["geloescht"] = weg.name
db.delete(weg)

db.add(Transaction(
    account_id=eigen.id, date=date(2026, 3, 14),
    amount=Decimal("-42.50"), description="Erfundener Einkauf",
))
db.commit()
"""

_NACHSEHEN = """
from sqlalchemy import func, select
from moneten.db.models import Account, Category, Transaction

ergebnis["eigenes_konto"] = db.scalar(
    select(func.count()).select_from(Account).where(Account.name == "Sparkonto Ferien")
)
ergebnis["buchungen"] = db.scalar(select(func.count()).select_from(Transaction))
ergebnis["kategorienamen"] = sorted(db.scalars(select(Category.name)).all())
"""


def test_ein_update_laesst_den_fremden_bestand_in_ruhe(frische_anlage: str) -> None:
    """Der Fall, der über die Weitergabe entscheidet.

    Jemand richtet sich die App ein: benennt eine Gruppe um, löscht eine
    Unterkategorie, legt ein eigenes Konto an, bucht. Dann kommt eine neue
    Fassung — also ein weiterer Start mit denselben Seeds. Danach muss sein
    Bestand unverändert dastehen.
    """
    _start(frische_anlage, _ZAEHLEN)                        # Erstinstallation
    seins = _start(frische_anlage, _SEINE_EINRICHTUNG)      # er richtet sich ein
    nachher = _start(frische_anlage, _NACHSEHEN)            # das Update

    assert nachher["eigenes_konto"] == 1, "Sein Konto ist verschwunden"
    assert nachher["buchungen"] == 1, "Seine Buchung ist verschwunden"
    assert "Meine eigene Gruppe" in nachher["kategorienamen"], (
        "Sein eigener Name wurde überschrieben"
    )
    assert seins["umbenannt_von"] not in nachher["kategorienamen"], (
        f"Die umbenannte Gruppe ist unter ihrem alten Namen "
        f"({seins['umbenannt_von']}) wieder da — jetzt gibt es sie zweimal."
    )
    assert seins["geloescht"] not in nachher["kategorienamen"], (
        f"Die gelöschte Kategorie ({seins['geloescht']}) kam beim Update zurück"
    )


# ---------------------------------------------------------------------------
# 3. Update über mehrere Fassungen hinweg
# ---------------------------------------------------------------------------
def test_wer_eine_fassung_ueberspringt_kommt_trotzdem_hoch(tmp_path) -> None:
    """Sie werden nicht jede Fassung mitnehmen.

    Geprüft wird der Sprung von einer älteren Marke bis zum Kopf. Eine leere
    Datenbank ist dabei kein schwacher Fall: die Kette enthält Schritte, die
    Tabellen umbauen, und die laufen unabhängig vom Inhalt.
    """
    url = f"sqlite:///{(tmp_path / 'alt.db').as_posix()}"
    _alembic("upgrade", "0021_lohnzusammensetzung", url=url)
    _alembic("upgrade", "head", url=url)

    e = _start(url, _ZAEHLEN)
    assert e["login"] == 200
    assert e["konten"] > 0 and e["kategorien"] > 0
