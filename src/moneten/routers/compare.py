"""Vergleichsansicht: Monat-zu-Monat und Jahr-zu-Jahr (reine Lese-Seite)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import User
from moneten.db.session import get_db
from moneten.services.comparison import build_comparison
from moneten.templating import templates

router = APIRouter(tags=["compare"])


@router.get("", response_class=HTMLResponse)
def compare_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Vergleich aktueller Monat vs. Vormonat + aktuelles Jahr vs. Vorjahr."""
    ctx = build_comparison(db, heute_lokal())
    ctx |= {"user": user, "active_tab": "compare"}
    return templates.TemplateResponse(request, "compare.html", ctx)
