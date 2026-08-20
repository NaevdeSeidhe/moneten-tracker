"""Jede Route hängt an ``require_login`` — ausser den namentlich genannten.

**Warum das ein Wächter sein muss.** Heute sind alle Routen geschützt; nachgezählt
wurde das von Hand. Der Fehler entsteht beim NÄCHSTEN Router: FastAPI meldet eine
fehlende Dependency nicht, die Seite funktioniert im Browser tadellos — man ist ja
angemeldet —, und ohne Cookie liefert sie dann Zahlen statt einer Weiterleitung.
Bei über hundert Routen fällt eine fehlende Zeile beim Lesen niemandem auf.
Gemessen fällt sie sofort auf.

**Die Ausnahmen stehen als exakte Pfade, nicht als Präfixe.** Ein Präfix
``/auth/webauthn`` deckte still auch ``/register/begin`` und ``/register/complete``
mit ab — also genau die zwei Routen, die geschützt sein MÜSSEN, weil sie einen
neuen Schlüssel an das Konto hängen.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from moneten.auth.pin import require_login
from moneten.main import app

#: Routen ohne Login-Zwang, jede mit ihrem Grund. Wer hier etwas einträgt, sagt
#: damit: „diese Seite darf ein Fremder ohne Cookie sehen."
OFFEN: dict[str, str] = {
    "/health": "Der Docker-Healthcheck fragt sie an — mit Login gäbe es keinen Healthcheck.",
    "/login": "Die Anmeldeseite selbst.",
    "/logout": "Abmelden muss auch mit abgelaufener Sitzung noch gehen.",
    "/auth/webauthn/authenticate/begin": "Passkey-Anmeldung — das ist der Weg HINEIN.",
    "/auth/webauthn/authenticate/complete": "Zweiter Halbschritt derselben Anmeldung.",
}
# Nicht in dieser Liste: ``/static`` und das PWA-Manifest. Beides kommt nicht aus
# einer Route, sondern aus dem eingehängten Dateiverzeichnis — ein Login-Zwang
# dort sperrte die Anmeldeseite aus, die ihr eigenes CSS lädt.


def _haengt_an_login(route: APIRoute) -> bool:
    """Sucht ``require_login`` in der gesamten Abhängigkeitskette der Route."""
    offen = [route.dependant]
    while offen:
        d = offen.pop()
        if d.call is require_login:
            return True
        offen.extend(d.dependencies)
    return False


def test_jede_route_verlangt_eine_anmeldung() -> None:
    ungeschuetzt = sorted(
        f"{sorted(r.methods)[0]} {r.path}"
        for r in app.routes
        if isinstance(r, APIRoute) and r.path not in OFFEN and not _haengt_an_login(r)
    )
    assert not ungeschuetzt, (
        "Diese Routen kommen ohne Anmeldung aus:\n  " + "\n  ".join(ungeschuetzt)
        + "\n\nIst das Absicht, gehört der Pfad mit einem Satz Begründung in OFFEN."
    )


def test_die_ausnahmeliste_ist_nicht_veraltet() -> None:
    """Ein Eintrag für eine Route, die es nicht mehr gibt, ist eine Einladung.

    Er sieht aus wie eine geprüfte Entscheidung, deckt aber nichts mehr ab —
    und wenn der Pfad später für etwas anderes wiederverwendet wird, ist er
    still offen.
    """
    vorhanden = {r.path for r in app.routes if isinstance(r, APIRoute)}
    verwaist = sorted(p for p in OFFEN if p not in vorhanden)
    assert not verwaist, f"Ausnahmen ohne Route: {verwaist}"


def test_passkey_registrierung_ist_geschuetzt() -> None:
    """Der Fall, der eine Präfix-Ausnahme still mit abgedeckt hätte.

    Wer einen Passkey registrieren kann, hängt einen zweiten Schlüssel an das
    Konto — das muss hinter der Anmeldung liegen.
    """
    heikel = {"/auth/webauthn/register/begin", "/auth/webauthn/register/complete"}
    gefunden = {r.path for r in app.routes if isinstance(r, APIRoute) and r.path in heikel}
    assert gefunden == heikel, f"Routen nicht gefunden: {heikel - gefunden}"
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path in heikel:
            assert _haengt_an_login(r), f"{r.path} ist ohne Anmeldung erreichbar"
