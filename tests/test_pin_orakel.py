"""Jede Tür, die eine PIN prüft, muss dieselbe Bremse haben.

Eine Bremse, die nur an einer von mehreren Türen gilt, ist keine Bremse: die
übrigen Türen prüfen dieselbe PIN, zählen aber nichts. Diese Datei misst, dass
der PIN-Wechsel denselben Zähler benutzt wie der Login.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from moneten.auth import drossel

PIN = "424242"


@pytest.fixture(autouse=True)
def _pin_zuruecklegen():
    """Der letzte Test hier wechselt die PIN WIRKLICH — sie muss zurück.

    Ohne das schlug jede spätere Anmeldung fehl: ``logged_in_client`` meldet
    sich mit ``424242`` an, und die galt nicht mehr. Die Folge waren über
    hundert Fehler in Dateien, die mit PINs nichts zu tun haben, mit „Falsche
    PIN" als Begründung — eine Spur, die in die Irre führt.

    Die Drossel leert die gemeinsame Vorbereitung in ``conftest.py``.
    """
    from sqlalchemy import select

    from moneten.db.models import User
    from moneten.db.session import SessionLocal

    with SessionLocal() as db:
        user = db.scalars(select(User)).first()
        vorher_hash, vorher_wann = user.pin_hash, user.pin_changed_at

    yield

    with SessionLocal() as db:
        user = db.scalars(select(User)).first()
        user.pin_hash, user.pin_changed_at = vorher_hash, vorher_wann
        db.commit()


def _versuch(client: TestClient, aktuell: str):
    return client.post(
        "/settings/pin",
        data={"current_pin": aktuell, "new_pin": "739104", "confirm_pin": "739104"},
    )


def test_der_pin_wechsel_ist_kein_orakel(logged_in_client: TestClient) -> None:
    """Nach zehn Fehlversuchen ist Schluss — wie am Login."""
    for nr in range(drossel.MAX_VERSUCHE):
        antwort = _versuch(logged_in_client, "000000")
        assert antwort.status_code == 400, f"Versuch {nr + 1}: {antwort.status_code}"

    antwort = _versuch(logged_in_client, "000000")
    assert antwort.status_code == 429, (
        f"Nach {drossel.MAX_VERSUCHE} Fehlversuchen antwortet die Route weiter mit "
        f"{antwort.status_code} — die aktuelle PIN lässt sich hier durchprobieren."
    )


def test_die_sperre_gilt_auch_fuer_die_richtige_pin(logged_in_client: TestClient) -> None:
    """Sonst wäre sie keine Sperre, sondern eine Verzögerung.

    Der Preis ist bekannt und gewollt: wer sich zehnmal vertippt, wartet ein
    paar Minuten. Das ist derselbe Handel wie am Login.
    """
    for _ in range(drossel.MAX_VERSUCHE):
        _versuch(logged_in_client, "000000")
    assert _versuch(logged_in_client, PIN).status_code == 429


def test_ein_erfolgreicher_wechsel_bleibt_moeglich(logged_in_client: TestClient) -> None:
    """Die Bremse darf den normalen Weg nicht verstellen."""
    antwort = _versuch(logged_in_client, PIN)
    assert antwort.status_code == 200, antwort.text
