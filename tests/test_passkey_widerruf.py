"""Ein zweiter Anmeldefaktor braucht eine Bedingung — und einen Widerruf.

Ein Passkey ersetzt die PIN vollständig und überlebt einen PIN-Wechsel. Diese
Datei hält beide Zusagen fest: **anlegen nur mit PIN**, und **entfernen gibt es
überhaupt** — samt Knopf in den Einstellungen, denn ein Widerruf, den man nur
per HTTP-Aufruf erreicht, hat niemand.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from moneten.auth import drossel

PIN = "424242"


@pytest.fixture(autouse=True)
def _drossel_leeren():
    drossel.zuruecksetzen()
    yield
    drossel.zuruecksetzen()


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------
def test_ohne_pin_kein_neuer_passkey(logged_in_client: TestClient) -> None:
    """Der Fall aus dem Befund: gültige Sitzung, aber keine PIN."""
    antwort = logged_in_client.post("/auth/webauthn/register/begin")
    assert antwort.status_code == 403, (
        "Mit einer erbeuteten Sitzung liesse sich ein zweiter Anmeldefaktor anlegen"
    )


def test_falsche_pin_legt_keinen_passkey_an(logged_in_client: TestClient) -> None:
    antwort = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": "000000"})
    assert antwort.status_code == 403


def test_mit_richtiger_pin_beginnt_die_zeremonie(logged_in_client: TestClient) -> None:
    antwort = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": PIN})
    assert antwort.status_code == 200, antwort.text
    daten = antwort.json()
    assert "challenge" in daten and "rp" in daten and "user" in daten


def test_die_anfrage_verlangt_nutzerverifikation(logged_in_client: TestClient) -> None:
    """„preferred" ist eine Bitte, keine Bedingung.

    Ohne diese Zusage genügte blosse Anwesenheit am Gerät — ein Tastendruck.
    Der Passkey ersetzt hier die PIN vollständig; dann muss das Gerät den
    Menschen davor erkannt haben.
    """
    daten = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": PIN}).json()
    assert daten["authenticatorSelection"]["userVerification"] == "required", daten


# ---------------------------------------------------------------------------
# Die Bremse gilt auch hier
# ---------------------------------------------------------------------------
def test_diese_tuer_ist_kein_pin_orakel(logged_in_client: TestClient) -> None:
    """Sonst wäre die neue Bedingung nur eine neue Stelle zum Durchprobieren."""
    for _ in range(drossel.MAX_VERSUCHE):
        logged_in_client.post("/auth/webauthn/register/begin", json={"pin": "000000"})
    antwort = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": "000000"})
    assert antwort.status_code == 429, (
        f"Nach {drossel.MAX_VERSUCHE} Fehlversuchen antwortet die Route weiter mit "
        f"{antwort.status_code} — die PIN lässt sich hier durchprobieren."
    )


# ---------------------------------------------------------------------------
# Entfernen
# ---------------------------------------------------------------------------
def test_entfernen_verlangt_die_pin(logged_in_client: TestClient) -> None:
    assert logged_in_client.post("/auth/webauthn/entfernen").status_code == 403
    assert logged_in_client.post(
        "/auth/webauthn/entfernen", json={"pin": "000000"}
    ).status_code == 403


def test_entfernen_raeumt_wirklich_auf(logged_in_client: TestClient) -> None:
    """Mit erfundenen Zugangsdaten im Feld — die Zeremonie braucht es dafür nicht."""
    from sqlalchemy import select

    from moneten.db.models import User
    from moneten.db.session import SessionLocal

    with SessionLocal() as db:
        user = db.scalars(select(User)).first()
        user.webauthn_credentials_json = (
            '[{"id": "AAAA", "public_key": "BBBB", "sign_count": 0}]'
        )
        db.commit()

    assert logged_in_client.get("/auth/webauthn/registered").json() == {"count": 1}

    antwort = logged_in_client.post("/auth/webauthn/entfernen", json={"pin": PIN})
    assert antwort.status_code == 200, antwort.text
    assert antwort.json() == {"entfernt": 1}
    assert logged_in_client.get("/auth/webauthn/registered").json() == {"count": 0}


def test_die_einstellungen_bieten_beides_an(logged_in_client: TestClient) -> None:
    """Ein Widerruf, den man nur per HTTP-Aufruf erreicht, hat niemand."""
    seite = logged_in_client.get("/settings").text
    assert "wa-register" in seite
    assert "wa-entfernen" in seite, "Kein Knopf zum Entfernen der Passkeys"
    assert "wa-pin" in seite, "Kein Feld für die PIN, die das Anlegen verlangt"


# ---------------------------------------------------------------------------
# Was der Browser nach dem Abmelden noch weiss
# ---------------------------------------------------------------------------
def test_der_beleg_entwurf_wird_beim_abmelden_geloescht(client: TestClient) -> None:
    """Er hielt bis zu 24 Stunden Händler, Datum, Betrag und jede Position.

    Weder ``/logout`` noch eine abgelaufene Sitzung räumten ihn auf. Wer danach
    das Gerät aus der Hand gab, gab die letzte Quittung mit. Geprüft wird das
    Skript, nicht der Browser: die Anmeldeseite muss den Schlüssel entfernen.
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "src/moneten/static/js/app.js").read_text(
        encoding="utf-8"
    )
    stelle = js.find("login-shell")
    assert stelle > 0, "Kein Zweig, der auf der Anmeldeseite läuft"
    umgebung = js[stelle:stelle + 600]
    assert "removeItem" in umgebung and "beleg.entwurf" in umgebung, umgebung[:300]

    # Und die Seite muss den Haken tragen, an dem das hängt.
    assert "login-shell" in client.get("/login").text


# ---------------------------------------------------------------------------
# Was die Anmelde-Route preisgibt
# ---------------------------------------------------------------------------
def test_ohne_passkey_verraet_die_route_das_nicht(client: TestClient) -> None:
    """Sie trägt als einzige keine Anmeldung — sie ist ja der Anmeldeweg.

    Vorher antwortete sie ohne registrierten Passkey mit 400 „Kein Passkey
    registriert" und beantwortete damit jedem, der die Anmeldeseite erreicht,
    eine Frage, die ihn nichts angeht. Jetzt kommen reguläre Optionen ohne
    Einträge zurück; der Browser sagt dem Benutzer selbst, dass er nichts findet.
    """
    antwort = client.post("/auth/webauthn/authenticate/begin")
    assert antwort.status_code == 200, antwort.text
    daten = antwort.json()
    assert daten["allowCredentials"] == []
    assert "challenge" in daten


def test_die_anmelderoute_sperrt_den_pin_login_nicht(client: TestClient) -> None:
    """Eine Härtung, die den Besitzer aussperrt, ist eine Störung.

    Die Route wird beim normalen Anmelden aufgerufen — wer die Zeremonie
    abbricht, hat einen Aufruf, ohne etwas falsch gemacht zu haben. Auf den
    gemeinsamen Zähler gebucht, hätte er sich damit selbst vom PIN-Login
    ausgesperrt.
    """
    for _ in range(drossel.MAX_VERSUCHE + 2):
        client.post("/auth/webauthn/authenticate/begin")

    # Die Route selbst bremst …
    assert client.post("/auth/webauthn/authenticate/begin").status_code == 429
    # … der PIN-Login aber nicht.
    antwort = client.post("/login", data={"pin": PIN}, follow_redirects=False)
    assert antwort.status_code < 400, (
        f"Der PIN-Login antwortet mit {antwort.status_code} — die Passkey-Anfragen "
        "haben auf denselben Zähler gebucht."
    )
