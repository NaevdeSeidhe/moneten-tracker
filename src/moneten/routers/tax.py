"""Steuerjahr-Auszug — eine druckbare Seite pro Kalenderjahr.

Reine Auswertung, keine Mutation: es gibt nur ein GET. Die Seite ersetzt keine
Steuererklärung, sie liefert die Zahlen dafür.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import User
from moneten.db.session import get_db
from moneten.services.tax_year import steuer_uebersicht, steuerjahr
from moneten.templating import templates

router = APIRouter(tags=["tax"])


@router.get("", response_class=HTMLResponse)
def tax_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    jahr: int | None = None,
) -> Response:
    """Steuerjahr-Auszug. Ohne ``jahr`` das zuletzt abgeschlossene Jahr —
    danach sucht man im Frühling, nicht nach dem laufenden.

    Gibt es für dieses Jahr gar keine Buchungen, wird auf das jüngste Jahr mit
    Daten ausgewichen. Sonst begrüsste die Seite jeden, der die App noch kein
    volles Jahr benutzt, mit lauter Nullen — und keiner der Jahresknöpfe
    darunter wäre der gerade angezeigte, weil die Knöpfe nur Jahre mit
    Buchungen listen.

    ``uebersicht`` steht ÜBER dem Einzeljahr und darf ``None`` sein (weniger als
    zwei Jahre mit Buchungen). Sie hängt nicht am gewählten Jahr — welches Jahr
    unten aufgeschlagen ist, markiert das Template selbst.
    """
    heute = heute_lokal()
    # **Das Jahr kommt aus der Adresszeile.** ``date(jahr, 1, 1)`` wirft fuer
    # alles ausserhalb von 1..9999 einen ``ValueError``; ``?jahr=0`` oder
    # ``?jahr=99999`` ergaben damit einen Serverfehler. Ein unsinniges Jahr ist
    # aber kein Fehler der App, sondern eine unsinnige Eingabe — sie faellt auf
    # das zuletzt abgeschlossene Jahr zurueck.
    if jahr is not None and not (1 <= jahr <= 9999):
        jahr = None
    gewaehlt = jahr if jahr else heute.year - 1
    ctx = steuerjahr(db, gewaehlt)
    if jahr is None and gewaehlt not in ctx["verfuegbare_jahre"]:
        gewaehlt = ctx["verfuegbare_jahre"][0]
        ctx = steuerjahr(db, gewaehlt)
    ctx |= {"user": user, "active_tab": "tax", "uebersicht": steuer_uebersicht(db)}
    return templates.TemplateResponse(request, "tax.html", ctx)
