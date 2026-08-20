"""Beim Start der App fragt die Anmeldeseite von selbst nach dem Passkey.

**Warum.** Am Handy ist die Anmeldeseite nur dann zu sehen, wenn die Sitzung
abgelaufen ist. Der Knopf „Mit Passkey anmelden" war dort reine Handarbeit: erst
tippen, dann den Finger auflegen. Jetzt geht die Abfrage sofort auf.

**Die Ausnahme ist wichtiger als die Regel.** Wer sich ausdrücklich abmeldet,
darf nicht im selben Atemzug wieder hereingelassen werden — sonst wäre
„Abmelden" wirkungslos. Deshalb trägt der Logout-Redirect ``?abgemeldet=1``, und
nur ohne diesen Zusatz schaltet die Seite die automatische Abfrage ein.

Geprüft wird beides: der Schalter am Server und die Bedingungen im Browser-Code.
Die zweite Hälfte ist Textprüfung an ``app.js`` — ohne Gerät mit Fingerabdruck
lässt sich die Zeremonie nicht durchspielen, aber ihre Vorbedingungen schon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moneten.main import app

APP_JS = (Path(__file__).resolve().parents[1] / "src/moneten/static/js/app.js").read_text(encoding="utf-8")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _schalter(html: str) -> str:
    treffer = re.search(r'data-auto-passkey="(\d)"', html)
    assert treffer, "Die Anmeldeseite trägt keinen data-auto-passkey-Schalter mehr"
    return treffer.group(1)


def test_anmeldeseite_fragt_von_selbst(client: TestClient) -> None:
    """Der Normalfall: App gestartet, Sitzung abgelaufen, Finger drauf."""
    assert _schalter(client.get("/login").text) == "1"


def test_nach_dem_abmelden_fragt_sie_nicht(client: TestClient) -> None:
    """Sonst hätte „Abmelden" keine Wirkung — ein Fingerabdruck, und man ist zurück."""
    assert _schalter(client.get("/login?abgemeldet=1").text) == "0"


def test_abmelden_setzt_die_marke(client: TestClient) -> None:
    """Die Marke muss vom Logout KOMMEN, sonst nützt die Auswertung nichts."""
    antwort = client.get("/logout", follow_redirects=False)
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/login?abgemeldet=1"


def test_der_browser_code_kennt_die_vier_bedingungen() -> None:
    """Jede Bedingung verhindert einen konkreten Ärger — darum einzeln geprüft."""
    block = APP_JS[APP_JS.index("async function autoPasskey"):]
    block = block[:block.index("function initWebAuthn")]

    bedingungen = {
        "Schalter vom Server": 'dataset.autoPasskey !== "1"',
        "Browser kann Passkeys": "window.PublicKeyCredential",
        "eingebautes Verfahren vorhanden": "isUserVerifyingPlatformAuthenticatorAvailable",
        "in dieser Sitzung nicht schon weggewischt": '"wa-kein-auto"',
    }
    fehlend = [name for name, muster in bedingungen.items() if muster not in block]
    assert not fehlend, f"autoPasskey prüft nicht mehr: {fehlend}"


def test_autopasskey_wird_beim_seitenaufbau_gerufen() -> None:
    """Eine Funktion, die niemand aufruft, ist genau so gut wie keine."""
    block = APP_JS[APP_JS.index("function initWebAuthn"):]
    block = block[:block.index("\n  }", block.index("autoPasskey();") if "autoPasskey();" in block else 0) + 4]
    assert "autoPasskey();" in block, "initWebAuthn ruft autoPasskey nicht mehr auf"


def test_kein_conditional_mediation() -> None:
    """``mediation: "conditional"`` wäre die naheliegende, falsche Antwort.

    Sie zeigt den Passkey nur als Vorschlag in der Tastaturzeile — am Handy erst,
    nachdem man ein Feld angetippt hat. Das ist genau der Knopfdruck, der
    wegfallen sollte.
    """
    # Nur der AUFRUF zählt. Im Kommentar daneben steht der Begriff bewusst — er
    # erklärt, warum es diese Variante nicht ist.
    aufruf = re.search(r"navigator\.credentials\.get\(\{([^}]*)\}\)", APP_JS)
    assert aufruf, "Der Passkey-Login ruft credentials.get nicht mehr auf"
    assert "mediation" not in aufruf.group(1), (
        f"credentials.get bekommt eine mediation-Angabe: {aufruf.group(1).strip()}"
    )


def test_abgewischte_abfrage_meldet_keinen_fehler() -> None:
    """Ein weggewischter Dialog ist kein Fehler — er wurde ja nicht angefordert.

    Ohne diesen Zweig stünde nach jedem Start, bei dem er den Finger nicht
    auflegt, eine rote „Abgebrochen oder fehlgeschlagen"-Zeile auf der Seite.
    """
    block = APP_JS[APP_JS.index("async function loginPasskey"):]
    block = block[:block.index("async function autoPasskey")]
    assert "if (automatisch)" in block, "Der automatische Fall wird nicht mehr unterschieden"
    assert 'sessionStorage.setItem("wa-kein-auto"' in block, (
        "Nach dem Wegwischen fehlt die Marke — die Abfrage poppt beim nächsten "
        "Seitenaufbau wieder auf, gegen seinen erklärten Willen."
    )


def test_laufende_abfrage_ist_abbrechbar() -> None:
    """Sonst scheitert der Knopf an der noch offenen automatischen Abfrage.

    Der Browser lässt nur EINE WebAuthn-Zeremonie gleichzeitig zu; die zweite
    wirft „operation already in progress". Genau das träfe den Knopf, der als
    Rückfalllinie gedacht ist.
    """
    assert "AbortController" in APP_JS
    assert "signal: ctl.signal" in APP_JS
    assert 'e.name === "AbortError"' in APP_JS, (
        "Ein abgelöster Versuch würde als Fehlschlag angezeigt."
    )
