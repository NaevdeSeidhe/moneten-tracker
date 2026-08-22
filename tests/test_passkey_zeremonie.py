"""Die Passkey-Zeremonie einmal wirklich durchspielen — mit einem weichen Authenticator.

Die übrigen Passkey-Tests reichen erfundene JSON-Brocken ein und prüfen, dass die
App sie ablehnt. Das zeigt, dass Müll nicht durchkommt — nicht, dass ein
**gültiger** Ablauf an der richtigen Stelle scheitert. Genau dort lag der
Unterschied: die PIN-Pflicht sitzt an ``register/begin``, geprüft wird beim
Abschluss aber nur die Challenge. Wer sich eine gültige Challenge anderswo holt,
umgeht die Hürde — und ein Test mit Müll-Daten merkt davon nichts, weil er schon
an der Signatur scheitert.

Hier entsteht deshalb ein echtes Schlüsselpaar, echte ``authData`` mit gesetzten
Flags und ein CBOR-Attestationsobjekt der Art ``none``. Die App prüft alles davon
richtig durch. Damit misst diese Datei zwei Dinge:

* der legitime Weg (mit PIN) **funktioniert** — sonst wäre die Härtung nur eine
  kaputte Registrierung, und das merkt man sonst erst am Gerät;
* der Umweg über die Anmelde-Challenge **funktioniert nicht**.
"""

from __future__ import annotations

import hashlib
import json
import os

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url

PIN = "424242"
RP_ID = "testserver"
ORIGIN = "http://testserver"

# Flags im authData: UP (Anwesenheit), UV (Nutzer verifiziert), AT (Schlüssel dabei).
# UV muss gesetzt sein — die App verlangt Nutzerverifikation auch beim Anlegen.
_FLAGS = 0x01 | 0x04 | 0x40


def _cose_key(pub) -> bytes:
    """Öffentlicher Schlüssel im COSE-Format (ES256), wie ihn ein Authenticator liefert."""
    nums = pub.public_numbers()
    return cbor2.dumps({
        1: 2, 3: -7, -1: 1,
        -2: nums.x.to_bytes(32, "big"),
        -3: nums.y.to_bytes(32, "big"),
    })


def _mach_credential(challenge_b64: str) -> dict:
    """Ein weicher Authenticator: Schlüsselpaar, authData, Attestation ``none``."""
    key = ec.generate_private_key(ec.SECP256R1())
    cred_id = os.urandom(32)

    client_data = json.dumps(
        {"type": "webauthn.create", "challenge": challenge_b64,
         "origin": ORIGIN, "crossOrigin": False},
        separators=(",", ":"),
    ).encode()

    auth_data = (
        hashlib.sha256(RP_ID.encode()).digest()
        + bytes([_FLAGS])
        + (0).to_bytes(4, "big")      # Zähler
        + b"\x00" * 16                # AAGUID
        + len(cred_id).to_bytes(2, "big")
        + cred_id
        + _cose_key(key.public_key())
    )
    att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
    return {
        "id": bytes_to_base64url(cred_id),
        "rawId": bytes_to_base64url(cred_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "attestationObject": bytes_to_base64url(att_obj),
        },
        "clientExtensionResults": {},
    }


@pytest.fixture(autouse=True)
def _passkeys_leeren():
    """Vorher UND nachher leeren — die Suite teilt eine Datenbank.

    Ohne das erbt der zweite Test den Schlüssel des ersten und misst eine
    Ausgangslage, die es im Betrieb so nicht gibt; und die Dateien danach
    fänden einen Passkey vor, den sie nicht angelegt haben.
    """
    from sqlalchemy import select

    from moneten.db.models import User
    from moneten.db.session import SessionLocal

    def leeren():
        with SessionLocal() as db:
            user = db.scalars(select(User)).first()
            if user is not None:
                user.webauthn_credentials_json = None
                db.commit()

    leeren()
    yield
    leeren()


def test_mit_pin_laesst_sich_ein_passkey_wirklich_anlegen(logged_in_client: TestClient) -> None:
    """Der legitime Weg muss durchgehen — sonst ist die Härtung ein Defekt."""
    c = logged_in_client
    assert c.get("/auth/webauthn/registered").json() == {"count": 0}

    begin = c.post("/auth/webauthn/register/begin", json={"pin": PIN})
    assert begin.status_code == 200, begin.text

    antwort = c.post("/auth/webauthn/register/complete",
                     json=_mach_credential(begin.json()["challenge"]))
    assert antwort.status_code == 200, antwort.text
    assert c.get("/auth/webauthn/registered").json() == {"count": 1}


def test_eine_anmelde_challenge_taugt_nicht_zum_anlegen(logged_in_client: TestClient) -> None:
    """Der Umweg, der die PIN-Pflicht aushebelte.

    Ablauf des Angriffs: mit einer erbeuteten Sitzung ``authenticate/begin``
    aufrufen — das verlangt weder Anmeldung noch PIN und setzt trotzdem eine
    gültige Challenge. Mit ihr die Registrierung abschliessen. Vor der Bindung
    der Challenge an ihren Zweck legte das einen Passkey an, mit dem sich der
    Angreifer fortan **ohne PIN** anmeldete.
    """
    c = logged_in_client
    assert c.get("/auth/webauthn/registered").json() == {"count": 0}

    # Die PIN-Hürde steht.
    assert c.post("/auth/webauthn/register/begin").status_code == 403

    # Die Anmelde-Route gibt trotzdem eine Challenge heraus — das ist ihr Zweck.
    anmeldung = c.post("/auth/webauthn/authenticate/begin")
    assert anmeldung.status_code == 200, anmeldung.text

    antwort = c.post("/auth/webauthn/register/complete",
                     json=_mach_credential(anmeldung.json()["challenge"]))
    assert antwort.status_code == 400, (
        f"Mit einer Anmelde-Challenge liess sich ein Passkey anlegen ({antwort.status_code}) — "
        "die PIN-Pflicht beim Anlegen ist damit wirkungslos."
    )
    assert c.get("/auth/webauthn/registered").json() == {"count": 0}


def test_die_anmeldeseite_verraet_keine_schluessel(logged_in_client: TestClient) -> None:
    """Die Zusage, um die es geht — und zwar MIT eingerichtetem Passkey.

    Der frühere Test prüfte nur den leeren Fall; die Antwort ohne Passkey ist
    aber gar nicht die interessante. Verraten wurde etwas erst, wenn welche da
    sind: Anzahl der Geräte und stabile Kennungen, für jeden, der die
    Anmeldeseite erreicht — ohne Anmeldung, ohne PIN.
    """
    c = logged_in_client
    begin = c.post("/auth/webauthn/register/begin", json={"pin": PIN})
    c.post("/auth/webauthn/register/complete", json=_mach_credential(begin.json()["challenge"]))
    assert c.get("/auth/webauthn/registered").json() == {"count": 1}, "Aufbau missglückt"

    antwort = c.post("/auth/webauthn/authenticate/begin")
    assert antwort.status_code == 200, antwort.text
    daten = antwort.json()
    assert not daten.get("allowCredentials"), (
        f"Die Anmeldeseite nennt die eingerichteten Schlüssel: {daten.get('allowCredentials')}"
    )
    assert daten["userVerification"] == "required"


def test_neue_schluessel_muessen_auffindbar_sein(logged_in_client: TestClient) -> None:
    """Ohne Liste findet das Gerät nur einen auffindbar abgelegten Schlüssel.

    Wäre das nur „bevorzugt", entstünde womöglich einer, mit dem sich die App
    später nicht anmelden kann — und das merkt man erst am Gerät.
    """
    daten = logged_in_client.post("/auth/webauthn/register/begin", json={"pin": PIN}).json()
    assert daten["authenticatorSelection"]["residentKey"] == "required", daten
    assert daten["authenticatorSelection"]["userVerification"] == "required", daten
