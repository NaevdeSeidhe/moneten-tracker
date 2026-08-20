"""PIN-Authentifizierung.

* PIN wird mit Argon2id gehasht in ``users.pin_hash`` abgelegt.
* Login erzeugt ein signiertes Session-Cookie (``itsdangerous``).
* ``require_login`` ist die FastAPI-Dependency, die jede geschützte Route benutzt.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.config import settings
from moneten.db.models import User
from moneten.db.session import get_db

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()
_signer = TimestampSigner(settings.secret_key, salt="moneten-session")

_PIN_RE = re.compile(r"^\d{6}$")


# ---------------------------------------------------------------------------
# Hashing / Verifikation
# ---------------------------------------------------------------------------


def hash_pin(pin: str) -> str:
    """Erzeugt einen Argon2id-Hash der PIN."""
    return _hasher.hash(pin)


def verify_pin(pin: str, pin_hash: str) -> bool:
    """Prüft eine PIN gegen den gespeicherten Hash."""
    try:
        _hasher.verify(pin_hash, pin)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        # Auch ein BESCHAEDIGTER Hash ist ein fehlgeschlagener Login, kein
        # Serverfehler. Vorher wurde daraus ein 500 — und damit eine
        # Fehlermeldung statt einer Anmeldeseite, aus der niemand herausfindet.
        return False
    return True


def validate_pin_format(pin: str) -> None:
    """Wirft ``HTTPException`` wenn das PIN-Format nicht stimmt."""
    if not _PIN_RE.fullmatch(pin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PIN muss exakt 6 Ziffern enthalten.",
        )


# ---------------------------------------------------------------------------
# Session-Cookies
# ---------------------------------------------------------------------------


def sitzungsmarke(user: User) -> str:
    """Der Wert, der eine Sitzungs-Generation kennzeichnet.

    **Wozu.** Das Cookie trug bisher nur die Benutzernummer. Wer es einmal
    kopiert hatte, blieb drin: Abmelden löscht es nur im fremden Browser, und
    ein PIN-Wechsel änderte daran nichts. Gemessen — ein kopiertes Cookie war
    nach beidem weiter gültig, und mit ihm liess sich die PIN erneut ändern.

    Jetzt wandert der Zeitpunkt des letzten PIN-Wechsels in die Signatur. Ein
    Wechsel macht damit JEDE ältere Sitzung ungültig — auch die, von der man
    nichts weiss. Das ist der Grund, warum „PIN ändern" überhaupt hilft.
    """
    wann = user.pin_changed_at
    return wann.isoformat() if wann else "start"


def cookie_name() -> str:
    """Der Name des Sitzungs-Cookies — im Betrieb mit ``__Host-`` davor.

    ``__Host-`` ist eine Zusage, die der Browser durchsetzt: das Cookie muss
    ``Secure`` sein, ``Path=/`` haben und darf keine Domain tragen. Damit kann es
    nur vom genau selben Rechnernamen stammen und nicht von einem Nachbardienst
    unter derselben Namensendung.

    Im Entwicklungsmodus bleibt der schlichte Name: dort läuft die App über
    ``http``, und ein ``__Host-``-Cookie ohne ``Secure`` verwirft der Browser
    kommentarlos.
    """
    if settings.dev_mode:
        return settings.session_cookie_name
    return f"__Host-{settings.session_cookie_name}"


def issue_session(response: Response, user_id: int, marke: str = "start") -> None:
    """Setzt das signierte Session-Cookie auf der Response."""
    token = _signer.sign(f"{user_id}:{marke}").decode("utf-8")
    response.set_cookie(
        key=cookie_name(),
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=not settings.dev_mode,
        samesite="lax",
        path="/",
    )


def refresh_session(request: Request, response: Response) -> None:
    """Setzt die Ablauffrist zurück, wenn eine gültige Sitzung vorliegt.

    Macht aus der festen Frist ein gleitendes Fenster: Wer die App benutzt,
    bleibt angemeldet; abgelaufen ist sie erst nach echter Untätigkeit. Ohne das
    würde eine 15-Minuten-Frist mitten im Arbeiten zuschlagen — und man würde sie
    nach zwei Tagen wieder hochdrehen.

    Bewusst NICHT hier: ein „Angemeldet bleiben"-Kästchen. Zwei Fristen sind zwei
    Verhaltensweisen, die man auseinanderhalten muss, und die längere gewinnt im
    Zweifel immer.
    """
    # Hat die Route selbst schon eine Sitzung gesetzt, bleibt sie stehen. Ohne
    # diese Zeile überschrieb das gleitende Fenster nach einem PIN-Wechsel das
    # frische Cookie mit dem alten — und der Benutzer flog durch seine eigene
    # Änderung sofort wieder hinaus.
    if any(settings.session_cookie_name in wert.decode("latin-1")  # deckt beide Namen ab
           for schluessel, wert in response.raw_headers
           if schluessel.lower() == b"set-cookie"):
        return
    gelesen = _read_session(request)
    if gelesen is not None:
        user_id, marke = gelesen
        issue_session(response, user_id, marke)


def clear_session(response: Response) -> None:
    """Löscht das Session-Cookie."""
    # **Mit denselben Eigenschaften löschen, mit denen gesetzt wurde.** Ein
    # ``__Host-``-Cookie nimmt der Browser nur mit ``Secure`` an — auch das
    # Lösch-Cookie. Ohne das Flag verwirft er die Löschung stillschweigend, und
    # die Sitzung überlebt das Abmelden.
    #
    # Beide Namen: nach einem Wechsel zwischen Betrieb und Entwicklung liegt
    # sonst noch das Cookie unter dem anderen Namen im Browser.
    for name in (cookie_name(), settings.session_cookie_name):
        response.delete_cookie(
            name, path="/", httponly=True,
            secure=not settings.dev_mode, samesite="lax",
        )


def _read_session(request: Request) -> tuple[int, str] | None:
    """Liest Benutzernummer und Sitzungsmarke aus dem Cookie.

    Gibt ``None`` zurück, wenn nichts, etwas Ungültiges oder etwas Abgelaufenes
    dasteht. Alte Cookies ohne Marke (aus der Zeit vor der Widerrufbarkeit)
    gelten als Marke ``start`` — sie laufen damit beim nächsten PIN-Wechsel ab,
    statt sofort alle Sitzungen zu beenden.
    """
    # Beide Namen lesen: beim Umstellen auf ``__Host-`` liegt im Browser noch
    # das alte Cookie. Ohne diese Zeile wäre jede laufende Sitzung sofort weg —
    # eine Härtung, die den Benutzer aussperrt, wird beim nächsten Mal weggelassen.
    raw = request.cookies.get(cookie_name()) or request.cookies.get(
        settings.session_cookie_name
    )
    if not raw:
        return None
    try:
        value = _signer.unsign(raw, max_age=settings.session_max_age_seconds).decode("utf-8")
    except (BadSignature, SignatureExpired):
        return None
    nummer, _, marke = value.partition(":")
    try:
        return int(nummer), (marke or "start")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# FastAPI-Dependencies
# ---------------------------------------------------------------------------


def current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    """Optionale Variante: gibt User zurück, wenn eingeloggt, sonst ``None``.

    Für UI-Routen, die je nach Login-Status anders rendern (z.B. der
    Theme-Toggle in der Sidebar).
    """
    return _benutzer_der_sitzung(request, db)


def _benutzer_der_sitzung(request: Request, db: Session) -> User | None:
    """Benutzer zum Cookie — oder ``None``, wenn die Sitzung überholt ist.

    Die Marke im Cookie muss zum aktuellen Stand des Benutzers passen. Stimmt
    sie nicht, wurde die PIN seither gewechselt: das Cookie stammt dann aus der
    Zeit davor und gilt nicht mehr. Ohne diesen Vergleich wäre ein einmal
    kopiertes Cookie unbegrenzt gültig, weil jede benutzte Seite die Frist
    verlängert.
    """
    gelesen = _read_session(request)
    if gelesen is None:
        return None
    user_id, marke = gelesen
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None or sitzungsmarke(user) != marke:
        return None
    return user


# Solange die Start-PIN gilt, sind NUR diese Wege offen. Sonst käme man nicht
# einmal zur Wechsel-Seite — und Abmelden muss immer möglich bleiben.
PIN_WECHSEL_PFAD = "/pin-aendern"
_OFFEN_TROTZ_START_PIN = (PIN_WECHSEL_PFAD, "/logout", "/login", "/health", "/static")


def pin_ist_startwert(user: User) -> bool:
    """Gilt für diesen Benutzer noch die PIN aus der Konfiguration?

    Gefragt wird nicht nach der PIN selbst, sondern ob je eine eigene gesetzt
    wurde. Das ist die ehrlichere Frage: wer die Start-PIN bewusst noch einmal
    einträgt, hat sie immerhin gewählt — wer sie nie angefasst hat, weiss
    vielleicht gar nicht, dass sie in einer Beispieldatei nachzulesen ist.
    """
    return user.pin_changed_at is None


def require_login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Dependency: lädt den eingeloggten User oder wirft 401.

    Mit dem 401 zusammen wird der Header ``HX-Redirect: /login`` gesetzt,
    damit HTMX-Requests sauber umleiten. Ein normaler Browser-Request
    bekommt zusätzlich eine ``Location`` für die Server-Redirect-Behandlung
    durch den exception handler in ``main.py``.

    **Gilt noch die Start-PIN, endet der Weg hier** — 403 mit Verweis auf die
    Wechsel-Seite. Bewusst in der Dependency und nicht als Hinweisbanner: ein
    Banner klickt man weg, und die Regel gälte nur dort, wo jemand daran gedacht
    hat, sie einzubauen. Hier gilt sie für jede geschützte Route — auch für die,
    die es noch nicht gibt.
    """
    user = _benutzer_der_sitzung(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bitte einloggen.",
            headers={"HX-Redirect": "/login", "Location": "/login"},
        )
    if pin_ist_startwert(user) and not request.url.path.startswith(_OFFEN_TROTZ_START_PIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bitte zuerst eine eigene PIN setzen.",
            headers={"HX-Redirect": PIN_WECHSEL_PFAD, "Location": PIN_WECHSEL_PFAD},
        )
    return user
