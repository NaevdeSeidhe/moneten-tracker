"""Routen für PIN-Login und Logout.

* ``GET  /login``  — rendert die Login-Seite (PIN-Pad).
* ``POST /login``  — prüft die PIN, setzt Session-Cookie, leitet weiter.
* ``GET  /logout`` — löscht die Session und leitet auf ``/login``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.drossel import (
    FENSTER_SEKUNDEN,
    MAX_SCHLUESSEL,
    MAX_VERSUCHE,
    _fehlversuche,
    absender,
    fehlversuch_merken,
    zu_viele_versuche,
    zuruecksetzen,
)
from moneten.auth.pin import (
    clear_session,
    current_user,
    hash_pin,
    issue_session,
    pin_ist_startwert,
    require_login,
    sitzungsmarke,
    validate_pin_format,
    verify_pin,
)
from moneten.db.models import User
from moneten.db.session import get_db
from moneten.templating import templates

router = APIRouter(tags=["auth"])

_PIN_SECHS = re.compile(r"^\d{6}$")

# --- Die Bremse gegen durchprobierte PINs ----------------------------------
#
# Sie lag frueher HIER und galt damit fuer genau eine Route. Jetzt steht sie in
# `auth/drossel.py`, und jede Tuer, die eine PIN prueft, benutzt dieselbe.
#
# Die alten Namen bleiben als Verweis stehen: mehrere Tests greifen darauf zu,
# und sie zeigen auf dieselben Objekte — eine Umbenennung haette hier nichts
# verbessert und Tests unnoetig angefasst.
_absender = absender
_too_many_attempts = zu_viele_versuche
_record_failure = fehlversuch_merken
_clear_failures = zuruecksetzen
_fail_times = _fehlversuche
_FAIL_WINDOW = FENSTER_SEKUNDEN
_FAIL_MAX = MAX_VERSUCHE
_FAIL_KEYS_MAX = MAX_SCHLUESSEL


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    user: Annotated[User | None, Depends(current_user)],
    abgemeldet: int = 0,
) -> Response:
    """Login-Seite. Wer bereits eingeloggt ist, wird direkt aufs Dashboard geschickt.

    ``abgemeldet`` steuert, ob die Seite von selbst nach dem Passkey fragt. Beim
    App-Start am Handy soll sie das — dort ist die Seite nur zu sehen, weil die
    Sitzung abgelaufen ist, und ein Knopfdruck dazwischen ist reine Handarbeit.
    Nach einem AUSDRUECKLICHEN Abmelden nicht: wer geht, will nicht im selben
    Atemzug wieder hereingelassen werden.
    """
    if user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"theme": "dark", "auto_passkey": not abgemeldet},
    )


@router.post("/login")
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    pin: Annotated[str, Form()],
) -> Response:
    """Prüft die PIN und gibt entweder einen Fehler-Partial oder einen Redirect zurück.

    Bei HTMX-Aufrufen kommt die Antwort als HTML-Fragment, sonst als Redirect.
    """
    # Drossel: zu viele Fehlversuche → kurz blocken (vor der teuren Hash-Prüfung).
    absender = _absender(request)
    if _too_many_attempts(absender):
        return templates.TemplateResponse(
            request,
            "partials/login_error.html",
            {"error": "Zu viele Fehlversuche. Bitte ein paar Minuten warten."},
            status_code=429,
        )

    validate_pin_format(pin)

    user = db.scalar(select(User).where(User.id == 1))
    if user is None or not verify_pin(pin, user.pin_hash):
        _record_failure(absender)
        return templates.TemplateResponse(
            request,
            "partials/login_error.html",
            {"error": "Falsche PIN. Bitte erneut versuchen."},
            status_code=400,
        )

    _clear_failures(absender)
    # Erfolg: Cookie setzen und auf Dashboard leiten.
    redirect_target = "/"
    if request.headers.get("HX-Request") == "true":
        # HTMX kann den Server-Redirect über den ``HX-Redirect``-Header auswerten.
        response = Response(status_code=204)
        issue_session(response, user.id, sitzungsmarke(user))
        response.headers["HX-Redirect"] = redirect_target
        return response

    response = RedirectResponse(url=redirect_target, status_code=303)
    issue_session(response, user.id, sitzungsmarke(user))
    return response


# ---------------------------------------------------------------------------
# Erst-Wechsel der PIN
# ---------------------------------------------------------------------------


def _ist_folge(pin: str) -> bool:
    """Lauter gleiche Ziffern oder eine durchgehende Auf-/Abwärtsreihe.

    Das sind die ersten Versuche, die jemand macht — und die einzigen, die sich
    ohne Wörterbuch benennen lassen. Weitergehende Regeln (keine Geburtsjahre,
    keine Postleitzahlen) wären geraten: sie sperrten Zahlen, die niemand
    probiert, und liessen die durch, die jeder probiert.
    """
    ziffern = [int(z) for z in pin]
    if len(set(ziffern)) == 1:
        return True
    # ``strict=False`` ist hier richtig und nicht Bequemlichkeit: die zweite
    # Folge ist um eins kuerzer, das ist der Sinn des Paarens.
    schritte = {b - a for a, b in zip(ziffern, ziffern[1:], strict=False)}
    return schritte in ({1}, {-1})


@router.get("/pin-aendern", response_class=HTMLResponse)
def pin_wechsel_seite(
    request: Request,
    user: Annotated[User, Depends(require_login)],
) -> Response:
    """Die Seite, auf der man landet, solange die Start-PIN gilt.

    ``require_login`` lässt diesen Pfad ausdrücklich durch — sonst schickte die
    Sperre einen im Kreis.
    """
    if not pin_ist_startwert(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "pin_erstwechsel.html", {"theme": "dark", "fehler": None}
    )


@router.post("/pin-aendern")
def pin_wechsel_speichern(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_login)],
    neue_pin: Annotated[str, Form()],
    bestaetigung: Annotated[str, Form()],
) -> Response:
    """Setzt die eigene PIN und hebt damit die Sperre auf.

    Die alte PIN wird hier NICHT abgefragt: man ist gerade damit hereingekommen,
    und eine zweite Abfrage derselben Zahl prüft nichts — sie kostet nur den
    dritten Tippfehler.

    **Deshalb gilt dieser Weg nur, solange die Start-PIN gilt.** Die GET-Route
    daneben prüft das seit jeher, diese hier nicht — und das war eine Umgehung:
    wer eine fremde Sitzung in die Hände bekam, konnte die PIN neu setzen, ohne
    die alte zu kennen. Aus geliehenem Zugriff wurde damit dauerhafter, und der
    Besitzer war ausgesperrt. Später führt der Weg über die Einstellungen, und
    dort verlangt die App die aktuelle PIN.
    """
    if not pin_ist_startwert(user):
        return RedirectResponse(url="/settings", status_code=303)

    def _fehler(text: str) -> Response:
        return templates.TemplateResponse(
            request, "pin_erstwechsel.html",
            {"theme": "dark", "fehler": text}, status_code=400,
        )

    if not _PIN_SECHS.fullmatch(neue_pin):
        return _fehler("Die PIN muss aus genau sechs Ziffern bestehen.")
    if neue_pin != bestaetigung:
        return _fehler("Die beiden Eingaben stimmen nicht überein.")
    if verify_pin(neue_pin, user.pin_hash):
        return _fehler("Das ist die bisherige PIN. Bitte eine andere wählen.")
    if _ist_folge(neue_pin):
        return _fehler("Keine durchgehende Reihe und keine sechs gleichen Ziffern.")

    user.pin_hash = hash_pin(neue_pin)
    user.pin_changed_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    db.refresh(user)
    # Frische Sitzung: der Wechsel entwertet ALLE aelteren Cookies (auch
    # kopierte) — und ohne diese Zeile auch das eigene, gerade benutzte.
    antwort = RedirectResponse(url="/", status_code=303)
    issue_session(antwort, user.id, sitzungsmarke(user))
    return antwort


@router.get("/logout")
def logout() -> Response:
    """Logout: Cookie löschen, zur Login-Seite umleiten.

    Mit ``?abgemeldet=1``, damit die Anmeldeseite nicht sofort von selbst nach
    dem Passkey fragt — sonst waere man mit einem Fingerabdruck wieder drin und
    das Abmelden hätte nichts bewirkt.
    """
    response = RedirectResponse(url="/login?abgemeldet=1", status_code=303)
    clear_session(response)
    return response
