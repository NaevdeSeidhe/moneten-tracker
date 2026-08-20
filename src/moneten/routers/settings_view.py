"""Settings-Seite: Theme-Toggle und PIN ändern."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from moneten import themes
from moneten.auth.drossel import absender, fehlversuch_merken, zu_viele_versuche
from moneten.auth.pin import (
    hash_pin,
    issue_session,
    require_login,
    sitzungsmarke,
    validate_pin_format,
    verify_pin,
)
from moneten.db.models import User
from moneten.db.session import get_db
from moneten.routers.auth_pin import _ist_folge
from moneten.services import anhang_tresor, artikelnamen, erkennung_pruefen, scan_protokoll
from moneten.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
) -> Response:
    """Übersichts-Seite der Einstellungen."""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "active_tab": "settings",
        },
    )


@router.post("/theme")
def set_theme(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    theme: Annotated[str, Form()],
) -> Response:
    """Speichert die Theme-Vorliebe am User-Datensatz.

    Seit 0.53 einachsig: ein Theme-NAME (dark | light | nord | …) statt
    dark/light plus separatem Reactor-Flag. Gültige Namen kommen aus
    :mod:`moneten.themes`; neue Farbwelten funktionieren hier ohne Codeänderung.
    Antwort ist leer (204) — das JS setzt ``data-theme`` sofort selbst.
    """
    if not themes.is_valid(theme):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekanntes Theme.")
    user.preferred_theme = theme.strip().lower()
    db.add(user)
    db.commit()
    return Response(status_code=204)



@router.post("/receipt-keep")
def set_receipt_keep(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    keep: Annotated[str, Form()] = "",
) -> Response:
    """Foto-Policy: reduziertes Beleg-Bild als Safety behalten (an) oder verwerfen (aus)."""
    user.receipt_photo_keep = keep in {"on", "1", "true"}
    db.add(user)
    db.commit()
    return Response(status_code=204)


@router.post("/pin", response_class=HTMLResponse)
def change_pin(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    current_pin: Annotated[str, Form()],
    new_pin: Annotated[str, Form()],
    confirm_pin: Annotated[str, Form()],
) -> Response:
    """Ändert die PIN nach Verifikation der alten PIN."""
    validate_pin_format(current_pin)
    validate_pin_format(new_pin)

    if new_pin != confirm_pin:
        return templates.TemplateResponse(
            request,
            "partials/pin_change_result.html",
            {"ok": False, "message": "Neue PIN und Bestätigung stimmen nicht überein."},
            status_code=400,
        )

    # Dieselbe Schranke wie beim Erst-Wechsel. Vorher ging `111111` hier durch
    # und dort nicht — eine Regel, die nur an einer von zwei Tueren gilt, ist
    # keine Regel.
    if _ist_folge(new_pin):
        return templates.TemplateResponse(
            request,
            "partials/pin_change_result.html",
            {"ok": False, "message": "Keine durchgehende Reihe und keine sechs gleichen Ziffern."},
            status_code=400,
        )

    # Dieselbe Bremse wie am Login: jede Tuer, die eine PIN prueft, zaehlt
    # Fehlversuche, sonst waere diese hier die ungebremste.
    wer = absender(request)
    if zu_viele_versuche(wer):
        return templates.TemplateResponse(
            request,
            "partials/pin_change_result.html",
            {"ok": False, "message": "Zu viele Fehlversuche. Bitte ein paar Minuten warten."},
            status_code=429,
        )

    if not verify_pin(current_pin, user.pin_hash):
        fehlversuch_merken(wer)
        return templates.TemplateResponse(
            request,
            "partials/pin_change_result.html",
            {"ok": False, "message": "Aktuelle PIN ist falsch."},
            status_code=400,
        )

    user.pin_hash = hash_pin(new_pin)
    # Auch hier mitschreiben: sonst gilt fuer die App weiter die Start-PIN, und
    # sie sperrt beim naechsten Aufruf jemanden aus, der gerade gewechselt hat.
    user.pin_changed_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    db.refresh(user)
    antwort = templates.TemplateResponse(
        request,
        "partials/pin_change_result.html",
        {"ok": True, "message": "PIN erfolgreich geändert."},
    )
    # Der Wechsel entwertet jede aeltere Sitzung — auch eine kopierte. Damit der
    # eigene Browser weiterlaeuft, bekommt er hier eine neue.
    issue_session(antwort, user.id, sitzungsmarke(user))
    return antwort


@router.post("/cash-goal", status_code=status.HTTP_204_NO_CONTENT)
def set_cash_goal(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    pct: Annotated[int, Form()],
) -> Response:
    """Gewünschter Bargeld-Anteil an den Alltagsausgaben (0 = kein Ziel).

    Auf 0…100 geklemmt statt abgewiesen: ein Tippfehler soll hier keinen Fehler
    produzieren, sondern den nächstliegenden sinnvollen Wert setzen.
    """
    user.cash_goal_pct = max(0, min(100, pct))
    db.add(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/name", status_code=status.HTTP_204_NO_CONTENT)
def set_name(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
) -> Response:
    """Anzeigename der einen Person, die diese App benutzt.

    **Warum das fehlte und warum es zählt.** Der Name stand nur im Seed
    (``"Ich"``) und war nirgends änderbar — auf der Einstellungsseite stand er
    als Text an genau der Stelle, an der man ihn ändern würde. Eine frisch
    aufgesetzte Installation begrüsste ihren neuen Besitzer also dauerhaft mit
    „Guten Abend, Ich".

    Leer eingegeben bleibt der alte Name stehen: ein versehentlich geleertes Feld
    soll die Begrüssung nicht abschneiden. Gekürzt wird auf die Spaltenbreite,
    damit ein zu langer Name nicht erst in der Datenbank auffällt.
    """
    sauber = name.strip()
    if sauber:
        user.name = sauber[:80]
        db.add(user)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/beleg-fotos", response_class=HTMLResponse)
def beleg_fotos_seite(
    request: Request,
    user: Annotated[User, Depends(require_login)],
) -> Response:
    """Die behaltenen Beleg-Fotos — der Weg, sie überhaupt noch anzusehen.

    Die Einstellung heisst „Reduziertes Bild als Safety auf dem NAS behalten",
    und genau so war sie gemeint: bei einer falsch erkannten Zahl geht man in
    die Dateifreigabe und sieht sich den Bon an. Seit die Bilder verschlüsselt
    abgelegt werden, öffnet sie dort nichts mehr — das Netz wäre für den
    Besitzer zu, wenn es diese Seite nicht gäbe.

    Sie ist ausserdem der bessere Ort: sie funktioniert vom Handy aus, und die
    Datei muss dafür nirgends im Klartext liegen.
    """
    ordner = anhang_tresor.foto_ordner()
    bilder = []
    if ordner.is_dir():
        for datei in sorted(ordner.iterdir(), reverse=True):
            if not datei.is_file() or datei.suffix == ".teil":
                continue
            bilder.append({
                "name": datei.name,
                "groesse": datei.stat().st_size // 1024,
                "wann": datetime.fromtimestamp(datei.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            })
    return templates.TemplateResponse(
        request, "beleg_fotos.html", {"bilder": bilder[:60], "gesamt": len(bilder)},
    )


@router.get("/beleg-foto/{name}")
def beleg_foto_ausliefern(
    user: Annotated[User, Depends(require_login)],
    name: str,
) -> Response:
    """Ein einzelnes Bild — entschlüsselt, wenn nötig.

    Der Name kommt aus der Adresszeile, wird also wie jede Eingabe behandelt:
    ausgeliefert wird nur, was nach dem Auflösen wirklich im Foto-Ordner liegt.
    """
    ordner = anhang_tresor.foto_ordner()
    try:
        ziel = (ordner / name).resolve()
        ziel.relative_to(ordner.resolve())
    except (ValueError, OSError):
        return Response(status_code=404)
    if not ziel.is_file():
        return Response(status_code=404)
    try:
        roh = anhang_tresor.lesen(ziel)
    except anhang_tresor.TresorFehler as fehler:
        # Kein 500: der Fall ist erklärbar (falscher oder fehlender Schlüssel),
        # und die Erklärung gehört zum Benutzer, nicht ins Protokoll.
        return Response(str(fehler), status_code=409, media_type="text/plain; charset=utf-8")
    return Response(roh, media_type="image/jpeg")


@router.get("/scan-protokoll", response_class=HTMLResponse)
def scan_protokoll_seite(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Die letzten Beleg-Erkennungen mit ihrem Rohtext.

    Eine eigene Seite und kein Aufklapper in den Einstellungen: wer hier landet,
    sucht einen Fehler und braucht Platz. Der Rohtext ist markierbar und steht
    in einem ``<pre>``, damit die Spalten des Belegs erhalten bleiben — genau
    so, wie die Erkennung ihn gesehen hat.
    """
    return templates.TemplateResponse(
        request, "scan_protokoll.html",
        {"eintraege": scan_protokoll.letzte(db), "grenze": scan_protokoll.MAX_EINTRAEGE},
    )


@router.get("/positionen", response_class=HTMLResponse)
def positionen_bereinigen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Schreibweisen, die vermutlich dieselbe Ware meinen.

    Gezeigt wird nur, wo es etwas zu entscheiden gibt — Gruppen mit mehr als
    einer Schreibweise. Eine Liste aller Positionen waere ein Bericht; hier soll
    man etwas tun koennen.
    """
    return templates.TemplateResponse(
        request, "positionen.html",
        {"buendel": artikelnamen.buendel(db), "aliase": artikelnamen.alias_karte(db)},
    )


@router.post("/positionen", response_class=HTMLResponse)
def positionen_vereinheitlichen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    kanonisch: Annotated[str, Form()] = "",
    variante: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI-Form-Default
) -> Response:
    """Schreibt eine Gruppe auf EINE Schreibweise um.

    Wirkt zweimal: der Bestand wird umgeschrieben (sonst fuehrte der
    Preisverlauf den Artikel weiter unter mehreren Namen) und jede Variante wird
    als Alias gemerkt (sonst kaeme derselbe Lesefehler beim naechsten Beleg
    wieder).
    """
    geaendert = artikelnamen.vereinheitliche(db, kanonisch, list(variante))
    return templates.TemplateResponse(
        request, "positionen.html",
        {
            "buendel": artikelnamen.buendel(db),
            "aliase": artikelnamen.alias_karte(db),
            "meldung": (f"{geaendert} Position{'en' if geaendert != 1 else ''} "
                        f"auf {kanonisch} umgeschrieben." if geaendert else None),
        },
    )


@router.get("/erkennung", response_class=HTMLResponse)
def erkennung_pruefstand(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Die heutige Erkennung noch einmal ueber alle gespeicherten Belegtexte.

    Der Bestand ist der ehrlichste Pruefstand, den es gibt: echte Belege, quer
    ueber Laeden und Papierqualitaeten. Eine Verbesserung am Parser liess sich
    vorher nur an dem einen Beleg pruefen, der gerade gemeldet war.
    """
    befunde = erkennung_pruefen.pruefe(db)
    return templates.TemplateResponse(
        request, "erkennung.html",
        {"befunde": befunde, "bilanz": erkennung_pruefen.bilanz(befunde)},
    )


@router.post("/erkennung", response_class=HTMLResponse)
def erkennung_neu_auswerten(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    beleg: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI-Form-Default
) -> Response:
    """Schreibt die Positionen der gewaehlten Belege aus ihrem Rohtext neu."""
    ids = [int(b) for b in beleg if b.isdigit()]
    geaendert = erkennung_pruefen.neu_auswerten(db, ids)
    befunde = erkennung_pruefen.pruefe(db)
    return templates.TemplateResponse(
        request, "erkennung.html",
        {
            "befunde": befunde,
            "bilanz": erkennung_pruefen.bilanz(befunde),
            "meldung": (f"{geaendert} Beleg{'e' if geaendert != 1 else ''} neu ausgewertet."
                        if geaendert else "Nichts geaendert."),
        },
    )
