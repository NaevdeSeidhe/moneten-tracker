"""Ein gestohlenes Cookie muss sich widerrufen lassen.

Der unabhängige Sicherheitsdurchgang hat nachgemessen, was vorher galt: ein
kopiertes Sitzungs-Cookie blieb nach dem Abmelden des Opfers gültig, es blieb
nach einem PIN-Wechsel gültig, und weil jede benutzte Seite die Frist
verlängert, blieb es beliebig lange gültig. Der einzige Widerruf war ein neuer
Signier-Schlüssel — also ein Neustart mit neuer Konfiguration.

Das Cookie trug nur die Benutzernummer. Jetzt trägt es zusätzlich den Zeitpunkt
des letzten PIN-Wechsels, und der wird bei jedem Aufruf gegen die Datenbank
geprüft. „PIN ändern" ist damit das, wofür man es hält: ein Riegel.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from moneten.db.models import User
from moneten.db.session import SessionLocal
from moneten.main import app

PIN = "424242"
NEUE_PIN = "907314"


@pytest.fixture
def pin_zuruecksetzen() -> Iterator[None]:
    """Hash und Wechselzeitpunkt nach dem Test wieder herstellen."""
    with SessionLocal() as db:
        user = db.get(User, 1)
        hash_vorher, wann_vorher = user.pin_hash, user.pin_changed_at
    yield
    with SessionLocal() as db:
        user = db.get(User, 1)
        user.pin_hash, user.pin_changed_at = hash_vorher, wann_vorher
        db.commit()


def test_pin_wechsel_entwertet_fremde_sitzungen(
    logged_in_client: TestClient, pin_zuruecksetzen
) -> None:
    """Der Kern: ein anderswo kopiertes Cookie gilt nach dem Wechsel nicht mehr.

    Nachgestellt wird der Diebstahl, indem der Cookie-Wert in einen zweiten
    Klienten übertragen wird — genau das, was ein Angreifer mit einem
    ausgelesenen Cookie tut.
    """
    gestohlen = logged_in_client.cookies.get("moneten_session")
    assert gestohlen, "ohne Cookie prüft dieser Test nichts"
    # Ein EIGENER Klient — die Fixture `client` waere dasselbe Objekt, und der
    # Test praefte dann nur sich selbst.
    dieb = TestClient(app)
    dieb.cookies.set("moneten_session", gestohlen)
    assert dieb.get("/transactions", follow_redirects=False).status_code == 200

    antwort = logged_in_client.post(
        "/settings/pin",
        data={"current_pin": PIN, "new_pin": NEUE_PIN, "confirm_pin": NEUE_PIN},
    )
    assert antwort.status_code == 200, antwort.text

    # Das gestohlene Cookie ist jetzt wertlos …
    assert dieb.get("/transactions", follow_redirects=False).status_code == 303
    # … und der eigene Browser bleibt drin (er hat beim Wechsel ein neues bekommen).
    assert logged_in_client.get("/transactions", follow_redirects=False).status_code == 200


def test_alte_sitzung_kann_die_pin_nicht_mehr_aendern(
    logged_in_client: TestClient, pin_zuruecksetzen
) -> None:
    """Sonst übernimmt der Dieb das Konto, statt nur mitzulesen.

    Gemessen war genau das möglich: mit dem kopierten Cookie liess sich die PIN
    setzen — und danach war der Betreiber draussen.
    """
    dieb = TestClient(app)
    dieb.cookies.set("moneten_session", logged_in_client.cookies.get("moneten_session"))
    logged_in_client.post(
        "/settings/pin",
        data={"current_pin": PIN, "new_pin": NEUE_PIN, "confirm_pin": NEUE_PIN},
    )
    versuch = dieb.post(
        "/settings/pin",
        data={"current_pin": NEUE_PIN, "new_pin": "135791", "confirm_pin": "135791"},
        follow_redirects=False,
    )
    assert versuch.status_code in (303, 401), versuch.status_code

    with SessionLocal() as db:
        from moneten.auth.pin import verify_pin

        assert verify_pin(NEUE_PIN, db.get(User, 1).pin_hash), "die fremde PIN wurde gesetzt"


def test_schwache_pin_auch_in_den_einstellungen_abgelehnt(
    logged_in_client: TestClient, pin_zuruecksetzen
) -> None:
    """Zwei Türen, eine Regel.

    Der Erst-Wechsel lehnte `111111` ab, die Einstellungen nahmen es an. Eine
    Regel, die nur an einer von zwei Türen gilt, ist keine.
    """
    for schwach in ("111111", "123456", "654321"):
        antwort = logged_in_client.post(
            "/settings/pin",
            data={"current_pin": PIN, "new_pin": schwach, "confirm_pin": schwach},
        )
        assert antwort.status_code == 400, f"{schwach} wurde angenommen"

    with SessionLocal() as db:
        from moneten.auth.pin import verify_pin

        assert verify_pin(PIN, db.get(User, 1).pin_hash), "die PIN wurde trotzdem geändert"


def test_beschaedigter_hash_ist_ein_fehlversuch_kein_serverfehler() -> None:
    """Ein kaputter Hash in der Datenbank darf keine 500er-Seite erzeugen.

    Wer dort landet, sieht eine Fehlermeldung statt einer Anmeldeseite und
    kommt aus eigener Kraft nicht mehr weiter.
    """
    from moneten.auth.pin import verify_pin

    assert verify_pin("424242", "kein-gueltiger-argon2-hash") is False
    assert verify_pin("424242", "") is False


def test_sitzungsmarke_haengt_am_wechselzeitpunkt() -> None:
    """Ohne diesen Zusammenhang wäre die Marke Zierde."""
    from moneten.auth.pin import sitzungsmarke

    ohne = User(id=99, pin_hash="x", pin_changed_at=None)
    mit = User(id=99, pin_hash="x", pin_changed_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    assert sitzungsmarke(ohne) == "start"
    assert sitzungsmarke(mit) != sitzungsmarke(ohne)
