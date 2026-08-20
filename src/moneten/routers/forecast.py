"""Prognose & Stresstest (Phase 4).

Eine Seite mit zwei Bereichen:
* **12-Monats-Vermögens-Prognose** (lineare Extrapolation, offline-SVG).
* **Stresstest** mit Slidern (Einkommen ±%, Ausgaben ±%, einmalige Ausgabe) →
  neuer Monatssaldo + Runway der liquiden Mittel. HTMX: Slider ändern → Teil-Neu-Render.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import Account, AccountType, User
from moneten.db.session import get_db
from moneten.services.forecasting import monthly_in_out, net_worth_projection, stresstest
from moneten.templating import chf, templates

router = APIRouter(tags=["forecast"])

# Liquide Mittel = Bank + Bargeld (gegen die der Runway läuft).
_LIQUID_TYPES = {AccountType.BANK, AccountType.CASH}


def _liquid_total(db: Session) -> Decimal:
    accounts = db.scalars(select(Account).where(Account.is_active.is_(True)))
    return sum((a.current_balance or Decimal("0") for a in accounts if a.type in _LIQUID_TYPES), Decimal("0"))


def _stress_context(db: Session, income_pct: int, expense_pct: int, one_time: Decimal) -> dict:
    """Baut den Kontext des Stresstest-Teils (für Voll-Seite und HTMX-Partial)."""
    today = heute_lokal()
    base_income, base_expense = monthly_in_out(db, today)
    liquid = _liquid_total(db)
    result = stresstest(
        base_income=base_income, base_expense=base_expense,
        income_pct=income_pct, expense_pct=expense_pct, one_time=one_time, liquid=liquid,
    )
    return {
        "base_income": base_income,
        "base_expense": base_expense,
        "liquid": liquid,
        "stress": result,
        "income_pct": income_pct,
        "expense_pct": expense_pct,
        "one_time": one_time,
    }


@router.get("", response_class=HTMLResponse)
def forecast_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Prognose-Seite: Vermögens-Extrapolation + Stresstest (Startwerte 0/0/0)."""
    proj = net_worth_projection(db, heute_lokal())
    sctx = _stress_context(db, 0, 0, Decimal("0"))
    proj_points = [
        {"x": p["x"], "y": p["y"], "label": p["label"], "value": chf(p["value"])}
        for p in proj.points
    ]
    # Konfig fürs Live-Update (Regler rechnen client-seitig, ohne Server-Roundtrip).
    stress_cfg = {
        "baseIncome": float(sctx["base_income"]),
        "baseExpense": float(sctx["base_expense"]),
        "liquid": float(sctx["liquid"]),
        "baseSaldo": float(sctx["stress"].base_saldo),
        "chart": {
            "pad": proj.pad, "w": proj.width, "h": proj.height,
            "lo": float(proj.lo), "span": float(proj.span),
            "histLen": proj.hist_len, "nTotal": proj.hist_len + proj.horizon,
            "lastVal": float(proj.last_value), "horizon": proj.horizon,
            # Rohwerte aller Stützpunkte (Historie + neutrale Prognose), damit der
            # Client beim Stresstest die Y-Skala mitwachsen lassen und alle Linien
            # neu zeichnen kann (statt die Szenario-Linie am Rand abzuschneiden).
            "vals": [float(p["value"]) for p in proj.points],
        },
    }
    ctx = {
        "user": user,
        "active_tab": "forecast",
        "projection": proj,
        "proj_points": proj_points,
        "stress_cfg": stress_cfg,
        **sctx,
    }
    return templates.TemplateResponse(request, "forecast.html", ctx)


def _to_int(raw: str, default: int = 0) -> int:
    try:
        return max(-90, min(200, int(float(raw))))  # plausible Grenzen
    except (ValueError, TypeError, OverflowError):
        # ``OverflowError`` gehoert dazu: ``float("inf")`` geht durch, erst
        # ``int()`` scheitert daran — und ``1e400`` wird beim Einlesen zu ``inf``.
        # Gemessen: beides ergab einen Serverfehler statt des Vorgabewerts.
        return default


@router.post("/stresstest", response_class=HTMLResponse)
def run_stresstest(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    income_pct: Annotated[str, Form()] = "0",
    expense_pct: Annotated[str, Form()] = "0",
    one_time: Annotated[str, Form()] = "0",
) -> Response:
    """Rechnet das Szenario neu und liefert nur den Stresstest-Teil zurück."""
    try:
        ot = Decimal(one_time.replace("'", "").replace(",", ".") or "0")
        if not ot.is_finite() or ot < 0:
            ot = Decimal("0")
    except (InvalidOperation, ValueError):
        ot = Decimal("0")
    ctx = _stress_context(db, _to_int(income_pct), _to_int(expense_pct), ot)
    return templates.TemplateResponse(request, "partials/forecast_stress.html", ctx)
