"""Preisverlauf: was einzelne Artikel früher gekostet haben.

Reine Lese-Seite — sie wertet nur aus, was beim Belege-Scannen ohnehin
gespeichert wurde. Nichts anzulegen, nichts zu bearbeiten, deshalb auch kein
HTMX-Wurzelcontainer wie sonst.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.db.models import User
from moneten.db.session import get_db
from moneten.services.price_history import preisverlauf
from moneten.templating import templates

router = APIRouter(tags=["prices"])

# Mehr als 40 Reihen liest niemand; die interessanten stehen dank Sortierung oben.
_MAX_ARTIKEL = 40


@router.get("", response_class=HTMLResponse)
def prices_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Artikel mit mindestens zwei Beobachtungen, stärkste Verteuerung zuerst."""
    # Erst zählen, dann kappen. Der Service sortiert nach Preisänderung absteigend,
    # abgeschnitten wird also das untere Ende — genau die stärksten Verbilligungen.
    # Auf der gekappten Liste gezählt, meldete die Karte darum „0 günstiger",
    # während in Wahrheit vieles billiger geworden war.
    alle = preisverlauf(db)
    anzahl_teurer = sum(1 for a in alle if a.diff > 0)
    anzahl_guenstiger = sum(1 for a in alle if a.diff < 0)
    artikel = alle[:_MAX_ARTIKEL]
    return templates.TemplateResponse(
        request,
        "prices.html",
        {
            "user": user,
            "active_tab": "prices",
            "artikel": artikel,
            "anzahl_teurer": anzahl_teurer,
            "anzahl_guenstiger": anzahl_guenstiger,
            "gekappt": len(alle) - len(artikel),
        },
    )
