"""WebAuthn / Passkey-Login (Fingerprint/Face am Handy, Touch ID am Laptop).

Ablauf (Standard-WebAuthn-Zeremonie in zwei Schritten):

* **Registrieren** (eingeloggt, in den Einstellungen): ``/register/begin`` liefert
  die Creation-Options + legt die Challenge in einem kurzlebigen, signierten
  Cookie ab. Der Browser erzeugt den Passkey, ``/register/complete`` prüft die
  Attestation und speichert den Credential in ``users.webauthn_credentials_json``.
* **Anmelden** (ohne PIN): ``/authenticate/begin`` liefert die Assertion-Options
  (mit den erlaubten Credentials), ``/authenticate/complete`` prüft die Signatur
  und setzt — bei Erfolg — dasselbe Session-Cookie wie der PIN-Login.

RP-ID und Origin werden aus dem Request abgeleitet (funktioniert hinter dem
Tailscale-HTTPS-Proxy genauso wie auf ``localhost``). Single-User: die Credentials
hängen am einzigen User.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from moneten.auth.drossel import absender, fehlversuch_merken, zu_viele_versuche, zuruecksetzen
from moneten.auth.pin import issue_session, require_login, sitzungsmarke, verify_pin
from moneten.config import settings
from moneten.db.models import User
from moneten.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/webauthn", tags=["auth"])

# Kurzlebiges signiertes Cookie für die Challenge zwischen begin/complete.
_wa_signer = TimestampSigner(settings.secret_key, salt="moneten-webauthn")
_WA_COOKIE = "wa_chal"
_WA_MAX_AGE = 300  # 5 Minuten reichen für die Zeremonie


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _rp_id(request: Request) -> str:
    """RP-ID = Hostname ohne Port, so wie der Browser ihn sieht.

    Aus der Anfrage und nicht aus der Konfiguration: ein Passkey ist an die
    Adresse gebunden, unter der er angelegt wurde. Stünde hier ein fester Wert,
    wäre er beim Wechsel der Adresse falsch — und ein Passkey, dessen RP-ID
    nicht zur aufgerufenen Adresse passt, wird vom Browser abgelehnt, ohne dass
    man sähe warum.
    """
    return request.url.hostname or "localhost"


def _origin(request: Request) -> str:
    """Origin = Schema + Host(+Port), wie ihn der Browser sieht."""
    return f"{request.url.scheme}://{request.url.netloc}"


def _load_credentials(user: User) -> list[dict]:
    """Liste der gespeicherten Passkeys des Users ([] falls keine)."""
    if not user.webauthn_credentials_json:
        return []
    try:
        data = json.loads(user.webauthn_credentials_json)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _save_credentials(db: Session, user: User, creds: list[dict]) -> None:
    user.webauthn_credentials_json = json.dumps(creds)
    db.add(user)
    db.commit()


def _set_challenge(response: Response, challenge: bytes, zweck: str) -> None:
    """Legt die Challenge ab — **mit ihrem Zweck**, nicht nur mit ihrem Wert.

    Ohne den Zweck ist jede Challenge für jede Zeremonie gültig. Das Anmelden
    braucht keine Anmeldung (es ist der Weg hinein) und liefert eine Challenge;
    dieselbe Challenge liesse sich dann beim Anlegen eines Passkeys einreichen,
    und die PIN-Pflicht dort wäre wirkungslos, weil sie nur an der
    ``begin``-Route sitzt. Der Zweck wird mitsigniert und beim Einlösen geprüft.
    """
    token = _wa_signer.sign(f"{zweck}:{bytes_to_base64url(challenge)}").decode("utf-8")
    response.set_cookie(
        _WA_COOKIE, token, max_age=_WA_MAX_AGE, httponly=True,
        secure=not settings.dev_mode, samesite="lax", path="/",
    )


def _read_challenge(request: Request, zweck: str) -> bytes | None:
    """Die Challenge — nur, wenn sie für GENAU diesen Zweck ausgestellt wurde."""
    raw = request.cookies.get(_WA_COOKIE)
    if not raw:
        return None
    try:
        wert = _wa_signer.unsign(raw, max_age=_WA_MAX_AGE).decode("utf-8")
        gestempelt, trenner, b64 = wert.partition(":")
        if not trenner or gestempelt != zweck:
            return None
        return base64url_to_bytes(b64)
    except (BadSignature, SignatureExpired, ValueError):
        return None


def _single_user(db: Session) -> User | None:
    """Die App ist Single-User — der eine Datensatz."""
    return db.scalar(select(User).order_by(User.id).limit(1))


def _pin_verlangen(request: Request, user: User, pin: str) -> None:
    """Ein zweiter Anmeldefaktor darf nicht mit einer geliehenen Sitzung entstehen.

    Ein Passkey ersetzt die PIN vollständig und überlebt einen PIN-Wechsel —
    das Anlegen muss deshalb dieselbe Bedingung erfüllen wie der PIN-Wechsel
    selbst: die aktuelle PIN. Die Drossel gilt hier mit.
    """
    wer = absender(request)
    if zu_viele_versuche(wer):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zu viele Fehlversuche. Bitte ein paar Minuten warten.",
        )
    if not pin or not verify_pin(pin, user.pin_hash):
        fehlversuch_merken(wer)
        logger.warning("Passkey-Aenderung ohne gueltige PIN abgelehnt.")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="PIN stimmt nicht.")


async def _pin_aus_dem_rumpf(request: Request) -> str:
    """Die PIN aus dem JSON-Rumpf — fehlt sie, ist das ein Fehlversuch wie jeder andere."""
    try:
        daten = await request.json()
    except Exception:  # noqa: BLE001 — kein Rumpf, kaputtes JSON: beides dasselbe
        return ""
    return str(daten.get("pin", "")) if isinstance(daten, dict) else ""


# ---------------------------------------------------------------------------
# Registrierung (eingeloggt)
# ---------------------------------------------------------------------------


@router.post("/register/begin")
async def register_begin(
    request: Request,
    user: Annotated[User, Depends(require_login)],
) -> Response:
    """Creation-Options für einen neuen Passkey + Challenge-Cookie.

    Verlangt die aktuelle PIN im Rumpf (``{"pin": "..."}``) — siehe
    :func:`_pin_verlangen`. Die Prüfung sitzt hier und nicht erst bei
    ``/register/complete``: dann bricht die Zeremonie ab, bevor der Browser
    seinen Fingerabdruck-Dialog öffnet.
    """
    _pin_verlangen(request, user, await _pin_aus_dem_rumpf(request))
    creds = _load_credentials(user)
    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name="Moneten-Tracker",
        user_id=str(user.id).encode("utf-8"),
        user_name=user.name or "ich",
        user_display_name=user.name or "Ich",
        # REQUIRED und nicht PREFERRED: „preferred" ist eine Bitte, keine
        # Bedingung. Ein Authenticator darf sie ignorieren, und dann genügt
        # blosse Anwesenheit — ein Tastendruck am Stick, ein Handy in fremder
        # Hand. Der Passkey ersetzt hier die PIN vollständig; dafür muss das
        # Gerät den Menschen davor erkennen (Fingerabdruck, Gesicht, Geräte-PIN).
        authenticator_selection=AuthenticatorSelectionCriteria(
            # REQUIRED und nicht PREFERRED: nur ein auffindbar abgelegter
            # Schluessel wird ohne Liste gefunden — und ohne Liste kommt die
            # Anmeldeseite aus, siehe :func:`authenticate_begin`. Waere er nur
            # „bevorzugt", entstuende womoeglich einer, mit dem sich die App
            # spaeter nicht anmelden kann; das merkt man erst am Geraet.
            #
            # Der Preis: aeltere USB-Sicherheitsschluessel ohne Speicher lehnen
            # das ab. Fuer Handy und Windows Hello ist es der Normalfall.
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in creds
        ],
    )
    resp = Response(content=options_to_json(options), media_type="application/json")
    _set_challenge(resp, options.challenge, "reg")
    return resp


@router.post("/register/complete")
async def register_complete(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Prüft die Attestation und speichert den neuen Passkey."""
    challenge = _read_challenge(request, "reg")
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Challenge abgelaufen — bitte erneut versuchen.")
    credential = await request.json()
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            # Auch beim ANLEGEN verlangt: sonst nimmt die App einen Schlüssel an,
            # mit dem sie sich später nie anmelden kann (die Anmeldung verlangt
            # Nutzerverifikation) — und der Besitzer merkt es erst dann.
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 — jede Verifikations-Fehlerart → 400
        logger.warning("WebAuthn-Registrierung fehlgeschlagen: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passkey-Registrierung ungültig.") from exc

    creds = _load_credentials(user)
    creds.append({
        "id": bytes_to_base64url(verification.credential_id),
        "public_key": bytes_to_base64url(verification.credential_public_key),
        "sign_count": verification.sign_count,
    })
    _save_credentials(db, user, creds)
    resp = Response(content='{"ok":true}', media_type="application/json")
    resp.delete_cookie(_WA_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------------------
# Anmeldung (ohne PIN)
# ---------------------------------------------------------------------------


@router.post("/authenticate/begin")
async def authenticate_begin(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Assertion-Options für den Passkey-Login + Challenge-Cookie.

    Diese Route trägt als einzige keine Anmeldung — sie ist der Anmeldeweg.
    Deshalb antwortet sie auch ohne registrierten Schlüssel mit regulären
    Optionen (leere Liste) statt mit einem Fehler, und sie hat einen eigenen
    Drossel-Zähler: getrennt vom PIN-Login, weil ein Abbruch der Zeremonie kein
    Fehlversuch ist.
    """
    # **Eigener Zaehler, nicht der des PIN-Logins.** Diese Route wird beim
    # normalen Anmelden aufgerufen, nicht nur beim Angriff: wer die Zeremonie
    # zweimal abbricht, hat zwei Aufrufe, ohne etwas falsch gemacht zu haben.
    # Auf den gemeinsamen Zaehler gebucht, haette er sich damit selbst vom
    # PIN-Login ausgesperrt — eine Haertung, die den Besitzer aussperrt, ist
    # eine Stoerung.
    wer = f"{absender(request)}|passkey-anfrage"
    if zu_viele_versuche(wer):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zu viele Versuche. Bitte ein paar Minuten warten.",
        )
    fehlversuch_merken(wer)

    # **Ohne Liste erlaubter Schluessel.** Sie stand hier, weil ein Geraet einen
    # nicht auffindbar abgelegten Schluessel sonst nicht findet. Sie verriet aber
    # jedem, der die Anmeldeseite erreicht, WIE VIELE Geraete eingerichtet sind
    # und unter welchen stabilen Kennungen — ohne dass er sich anmelden muesste.
    #
    # Seit die Registrierung auffindbare Schluessel verlangt
    # (``ResidentKeyRequirement.REQUIRED``), findet das Geraet seinen Schluessel
    # selbst. Wer noch einen aelteren, nicht auffindbaren hat, legt ihn einmal
    # neu an; hinein kommt er in der Zwischenzeit ueber die PIN.
    options = generate_authentication_options(
        rp_id=_rp_id(request),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    resp = Response(content=options_to_json(options), media_type="application/json")
    _set_challenge(resp, options.challenge, "auth")
    return resp


@router.post("/authenticate/complete")
async def authenticate_complete(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Prüft die Signatur und setzt bei Erfolg die Session (wie PIN-Login)."""
    challenge = _read_challenge(request, "auth")
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Challenge abgelaufen — bitte erneut versuchen.")
    user = _single_user(db)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Kein Benutzer.")
    credential = await request.json()
    creds = _load_credentials(user)
    match = next((c for c in creds if c["id"] == credential.get("id")), None)
    if match is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekannter Passkey.")
    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            credential_public_key=base64url_to_bytes(match["public_key"]),
            credential_current_sign_count=match.get("sign_count", 0),
            # Ohne diese Zeile ist die Anforderung oben eine Bitte geblieben:
            # py_webauthn prueft die Nutzerverifikation nur, wenn man es
            # ausdruecklich verlangt (Vorgabe ist False). Der Passkey ersetzt
            # die PIN — dann muss das Geraet den Menschen davor erkannt haben.
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebAuthn-Login fehlgeschlagen: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passkey-Login ungültig.") from exc

    match["sign_count"] = verification.new_sign_count  # Replay-Schutz aktualisieren
    _save_credentials(db, user, creds)
    resp = Response(content='{"ok":true}', media_type="application/json")
    # Erfolg raeumt den Zaehler dieser Route ab — sonst zaehlten die Aufrufe
    # eines normalen Tages weiter, bis die Sperre einen Berechtigten trifft.
    zuruecksetzen(f"{absender(request)}|passkey-anfrage")
    issue_session(resp, user.id, sitzungsmarke(user))
    resp.delete_cookie(_WA_COOKIE, path="/")
    return resp


@router.post("/entfernen")
async def passkeys_entfernen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Entfernt ALLE Passkeys — mit der aktuellen PIN als Bedingung.

    **Warum es das geben muss.** Frueher liess sich ein Passkey
    anlegen, aber durch nichts wieder entfernen: keine Route, kein Knopf, keine
    Anzeige ausser einer Zahl. Wer einen fremden Schlüssel im Konto vermutete,
    hatte genau eine Möglichkeit — die verschlüsselte Datenbank von Hand
    bearbeiten. Ein Faktor ohne Widerruf ist kein Faktor, sondern ein Risiko.

    **Warum alle auf einmal und nicht einzeln.** Die App gehört einer Person mit
    typischerweise ein bis zwei Geräten. Wer entfernt, tut es, weil etwas nicht
    stimmt — dann ist „alle weg, danach neu einrichten" die Handlung, die man
    ohne Nachdenken richtig macht. Eine Liste mit Kennungen zum Auswählen wäre
    genau im Ernstfall die schwierigere Entscheidung.
    """
    _pin_verlangen(request, user, await _pin_aus_dem_rumpf(request))
    vorher = len(_load_credentials(user))
    _save_credentials(db, user, [])
    logger.warning("Alle Passkeys entfernt (%d).", vorher)
    return {"entfernt": vorher}


@router.get("/registered")
async def webauthn_registered(
    user: Annotated[User, Depends(require_login)],
) -> dict:
    """Wie viele Passkeys sind registriert (für die Einstellungen-Anzeige)."""
    return {"count": len(_load_credentials(user))}
