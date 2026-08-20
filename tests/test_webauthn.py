"""Tests für die WebAuthn-Endpoints, soweit ohne echten Authenticator testbar.

Die eigentliche Krypto-Zeremonie (register/complete, authenticate/complete)
braucht einen echten Passkey-Authenticator und wird hier nicht simuliert.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_register_begin_returns_options(logged_in_client: TestClient) -> None:
    # Seither verlangt das Anlegen die aktuelle PIN — ein Passkey
    # ersetzt sie danach, also darf eine geliehene Sitzung dafuer nicht reichen.
    resp = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": "424242"})
    assert resp.status_code == 200
    data = resp.json()
    assert "challenge" in data and "rp" in data and "user" in data


def test_registered_count_zero(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/auth/webauthn/registered")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0}


def test_authenticate_begin_without_passkey(client: TestClient) -> None:
    """Ohne registrierten Passkey: reguläre Optionen OHNE Einträge — kein Fehler.

    Frueher antwortete die Route hier mit 400 „Kein Passkey
    registriert". Sie trägt als einzige keine Anmeldung (sie ist ja der
    Anmeldeweg) und beantwortete damit jedem, der die Anmeldeseite erreicht, die
    Frage, ob überhaupt einer eingerichtet ist. Der Browser sagt dem Benutzer
    jetzt selbst, dass er nichts Passendes findet.
    """
    resp = client.post("/auth/webauthn/authenticate/begin")
    assert resp.status_code == 200, resp.text
    assert resp.json()["allowCredentials"] == []


def test_login_shows_passkey_ui(client: TestClient) -> None:
    # Frischer (nicht eingeloggter) Client → Login-Seite mit Passkey-Button.
    assert "Mit Passkey anmelden" in client.get("/login").text


def test_settings_shows_passkey_ui(logged_in_client: TestClient) -> None:
    assert "Passkey auf diesem Gerät" in logged_in_client.get("/settings").text
