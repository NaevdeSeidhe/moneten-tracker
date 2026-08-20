"""Budget-Editor: Standard-Soll je Kategorie, Jahreskosten als Rückstellung.

Modell:
* **Standard-Soll** (`standard_budgets`) — einmal je Kategorie ausfüllen, gilt
  fortlaufend. Intervall **monatlich** oder **jährlich**.
* **Jährlich** → 1/12 fliesst ins Monatsbudget, die Position erscheint
  zusätzlich im Block **Rückstellungen** (volle Jahressumme + Monatsanteil).
* **Monats-Override** (`budgets`) — überschreibt den Standard für einen Monat.
* **Ist** — tatsächliche Ausgaben des Monats. **Ampel** = Soll/Ist-Verhältnis.
* **Median-Vorschlag** — Median der letzten 6 Monate als Startwert.

UI-Muster wie sonst: ein Container ``#budget-root``, der bei jeder Änderung
per HTMX neu gerendert wird.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import add_months, heute_lokal
from moneten.db.models import (
    Budget,
    BudgetInterval,
    Category,
    ManagementType,
    StandardBudget,
    User,
)
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.palette import color_at, icon_color_at
from moneten.services.budget_totals import anteil_prozent, monats_totale
from moneten.services.committed import offene_fixabgaenge
from moneten.services.median_budget import (
    ampel_status,
    autofill_standard_budgets,
    ist_map,
    median_from_map,
    monthly_equivalent,
)
from moneten.templating import MONATE, templates

router = APIRouter(tags=["budget"])

# Top-Kategorien, die KEIN Ausgaben-Budget bekommen.
_NON_BUDGET = {ManagementType.EINKOMMEN, ManagementType.TRANSFER}


def _parse_month(raw: str | None) -> date:
    """'YYYY-MM' → erster Tag des Monats. Default: aktueller Monat."""
    if raw:
        try:
            year, month = raw.split("-")
            return date(int(year), int(month), 1)
        except (ValueError, TypeError):
            pass
    return heute_lokal().replace(day=1)


def _build_view(db: Session, month_start: date, theme: str | None = None, info: str | None = None,
                ovr: int | None = None) -> dict:
    """Baut die komplette Budget-Ansicht: Gruppen, Summen, Allokation, Rückstellungen."""
    std_map = {sb.category_id: sb for sb in db.scalars(select(StandardBudget))}
    ovr_map = {
        b.category_id: b.planned_amount
        for b in db.scalars(select(Budget).where(Budget.month == month_start))
    }

    tops = db.scalars(
        select(Category).where(Category.parent_id.is_(None), Category.is_archived.is_(False)).order_by(Category.sort_order)
    ).all()

    # Ist-Werte für aktuellen Monat + 6 Vormonate in EINER Query vorberechnen
    # (statt ~7 Einzelabfragen pro Kategorie).
    imap = ist_map(db, add_months(month_start, -6), add_months(month_start, 1))

    groups: list[dict] = []
    rueckstellungen: list[dict] = []
    total_soll = Decimal("0")
    total_ist = Decimal("0")

    for top in tops:
        if top.management_type in _NON_BUDGET:
            continue
        subs = db.scalars(
            select(Category).where(Category.parent_id == top.id, Category.is_archived.is_(False)).order_by(Category.sort_order)
        ).all()
        rows = []
        grp_soll = Decimal("0")
        grp_ist = Decimal("0")
        for sub in subs:
            sb = std_map.get(sub.id)
            override = ovr_map.get(sub.id)
            interval = sb.interval if sb else BudgetInterval.MONATLICH
            is_yearly = sb is not None and sb.amount > 0 and interval == BudgetInterval.JAEHRLICH

            if override is not None and override > 0:
                eff = override
                source = "override"
            elif sb is not None and sb.amount > 0:
                eff = monthly_equivalent(sb.amount, interval)
                source = "standard"
            else:
                eff = None
                source = None

            ist = imap.get((sub.id, month_start), Decimal("0"))
            median = median_from_map(imap, sub.id, month_start)
            pct = int(float(ist) / float(eff) * 100) if (eff and eff > 0) else 0
            rows.append({
                "cat": sub,
                "std_amount": sb.amount if (sb and sb.amount > 0) else None,
                "interval": interval.value,
                "is_yearly": is_yearly,
                "annual": sb.amount if is_yearly else None,
                "eff": eff,
                "ist": ist,
                "median": median,
                "source": source,
                "has_override": source == "override",
                # Rohwert des Monats-Overrides + ob dessen Editor gerade offen ist
                # (analog zum Inline-Kategorie-Picker der Buchungsliste).
                "override": override,
                "edit_override": (ovr is not None and sub.id == ovr),
                "ampel": ampel_status(eff, ist),
                "pct": min(pct, 100),
                "over": pct > 100,
            })
            if eff:
                grp_soll += eff
            grp_ist += ist
            if is_yearly:
                rueckstellungen.append({
                    "cat": sub, "top": top.name,
                    "annual": sb.amount,
                    "monthly": monthly_equivalent(sb.amount, interval),
                })

        # Zeilen ohne Soll UND ohne Ausgabe sind auf dem Handy reines Rauschen:
        # sie füllen den Screen mit „CHF 0.00" und leeren Eingabefeldern. Sie
        # bleiben erreichbar, wandern aber hinter ein eigenes Aufklapp-Element.
        active = [r for r in rows if r["ist"] > 0 or (r["eff"] and r["eff"] > 0)]
        idle = [r for r in rows if not (r["ist"] > 0 or (r["eff"] and r["eff"] > 0))]

        grp_pct = int(float(grp_ist) / float(grp_soll) * 100) if grp_soll > 0 else 0
        groups.append({
            "top": top,
            "rows": rows,
            "active": active,
            "idle": idle,
            "soll": grp_soll,
            "ist": grp_ist,
            "rest": grp_soll - grp_ist,
            "pct": min(grp_pct, 100),
            "over": grp_ist > grp_soll and grp_soll > 0,
            "ampel": ampel_status(grp_soll or None, grp_ist),
            # Standardmässig offen sind nur Gruppen, die Aufmerksamkeit brauchen:
            # überzogen oder ab 80% verbraucht. Der Rest startet zugeklappt.
            "open": (grp_ist > grp_soll and grp_soll > 0) or grp_pct >= 80,
        })
        total_soll += grp_soll
        total_ist += grp_ist

    # Farbe hängt jetzt an der Gruppe selbst, nicht an ihrer Position im
    # Allokations-Balken: derselbe Ton trägt die Gruppe im Balken oben UND ihren
    # Anteilsbalken in der Karte. Vorher verschob sich die ganze Farbfolge,
    # sobald eine Gruppe ein Soll bekam oder verlor.
    for i, g in enumerate(groups):
        g["farbe"] = color_at(i, theme)
        g["icon_farbe"] = icon_color_at(i)

    # Ohne gesetztes Soll kann der Balken keinen Füllstand zeigen. Er trägt dann
    # den Anteil dieser Gruppe an den Monatsausgaben — die einzige Aussage, die
    # aus den Buchungen allein folgt (siehe anteil_prozent).
    monats_ist = sum((g["ist"] for g in groups), Decimal("0"))
    for g in groups:
        g["anteil"] = anteil_prozent(g["ist"], monats_ist) if g["soll"] <= 0 else 0

    # Weder Soll noch Ausgabe: eine solche Karte kann nichts zeigen ausser ihrem
    # Namen. Sie bleibt erreichbar (das Soll-Feld steckt darin), wandert aber
    # hinter ein Aufklapp-Element — gleiches Muster wie die stillen Zeilen INNERHALB
    # einer Gruppe.
    leere_gruppen = [g for g in groups if g["soll"] <= 0 and g["ist"] <= 0]
    groups = [g for g in groups if g["soll"] > 0 or g["ist"] > 0]

    # Allokations-Balken: nur Gruppen mit Soll — er zeigt die Planung, nicht die
    # Ausgaben. Felder passend zum split_bar-Makro.
    alloc = [
        {"label": g["top"].name, "value": g["soll"], "icon": g["top"].icon,
         "color": g["farbe"], "icon_color": g["icon_farbe"]}
        for g in groups if g["soll"] > 0
    ]
    alloc_total = sum((a["value"] for a in alloc), Decimal("0"))
    for a in alloc:
        a["pct"] = round(float(a["value"] / alloc_total * 100), 1) if alloc_total > 0 else 0

    # Die Leitzahl kommt aus dem gemeinsamen Service, damit die Schnell-Erfassen-
    # Seite garantiert dieselbe Zahl zeigt. Die Gruppenwerte oben bleiben die
    # Herleitung derselben Rechnung.
    gesamt = monats_totale(db, month_start)
    total_soll, total_ist = gesamt["soll"], gesamt["ist"]
    rest = gesamt["rest"]
    # Ein Teil der verbleibenden Luft ist schon vergeben: Fixkosten und Abos,
    # die diesen Monat noch abgehen. Ohne diesen Abzug wirkt Monatsmitte
    # entspannter, als sie ist.
    offen = offene_fixabgaenge(db, month_start)
    totals = {
        "soll": total_soll,
        "ist": total_ist,
        "rest": rest,
        "pct": min(int(float(total_ist) / float(total_soll) * 100), 100) if total_soll > 0 else 0,
        "over": total_ist > total_soll and total_soll > 0,
        # Ist nirgends ein Soll gesetzt, ist „Rest" der negative Ist-Betrag — die
        # Leitzahl meldete dann rot „Über Budget", obwohl nie ein Budget
        # existierte. In dem Fall trägt die Zahl das Ausgegebene.
        "kein_soll": total_soll <= 0,
        "vergeben": offen["summe"],
        "wirklich_frei": rest - offen["summe"],
        "offene_posten": offen["posten"],
    }
    rueck_annual = sum((r["annual"] for r in rueckstellungen), Decimal("0"))
    rueck_monthly = sum((r["monthly"] for r in rueckstellungen), Decimal("0"))

    month_str = f"{month_start.year:04d}-{month_start.month:02d}"
    return {
        "info": info,
        "groups": groups,
        "leere_gruppen": leere_gruppen,
        "totals": totals,
        "alloc": alloc,
        "alloc_total": alloc_total,
        "rueckstellungen": rueckstellungen,
        "rueck_annual": rueck_annual,
        "rueck_monthly": rueck_monthly,
        "month": month_str,
        "month_label": f"{MONATE[month_start.month - 1]} {month_start.year}",
        "prev_month": f"{add_months(month_start, -1):%Y-%m}",
        "next_month": f"{add_months(month_start, 1):%Y-%m}",
    }


def _render_root(request: Request, db: Session, month_start: date, theme: str | None = None,
                 info: str | None = None, ovr: int | None = None) -> Response:
    return templates.TemplateResponse(
        request, "partials/budget_root.html", _build_view(db, month_start, theme, info, ovr)
    )


@router.get("", response_class=HTMLResponse)
def budget_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    month: str | None = None,
    ovr: int | None = None,
) -> Response:
    """Budget-Seite für einen Monat.

    ``ovr=<category_id>`` öffnet den Monats-Override-Editor direkt in der
    betreffenden Zeile — gleiches Muster wie der Inline-Kategorie-Picker
    der Buchungsliste, damit die Zeile sonst schlank bleibt.
    """
    month_start = _parse_month(month)
    theme = user.preferred_theme  # Allokations-Balkenfarben ans aktive Theme koppeln
    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db, month_start, theme, ovr=ovr)
    ctx = _build_view(db, month_start, theme, ovr=ovr)
    ctx |= {"user": user, "active_tab": "budget"}
    return templates.TemplateResponse(request, "budget.html", ctx)


@router.post("/standard", response_class=HTMLResponse)
def set_standard_budget(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    category_id: Annotated[int, Form()],
    month: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    interval: Annotated[str, Form()] = "monatlich",
) -> Response:
    """Setzt (oder löscht) den **Standard-Soll** einer Kategorie."""
    month_start = _parse_month(month)
    try:
        betrag = parse_amount(amount)
    except InvalidOperation:
        return _render_root(request, db, month_start, user.preferred_theme)
    # Stale Ansicht (PWA-Restore/zweites Gerät): Kategorie kann gelöscht sein →
    # FK-Verletzung beim Commit → 500. Re-Render entfernt die tote Zeile stattdessen.
    if db.get(Category, category_id) is None:
        return _render_root(request, db, month_start, user.preferred_theme)

    iv = BudgetInterval.JAEHRLICH if interval == "jaehrlich" else BudgetInterval.MONATLICH
    existing = db.scalar(select(StandardBudget).where(StandardBudget.category_id == category_id))
    if betrag <= 0:
        if existing is not None:
            db.delete(existing)
    elif existing is not None:
        existing.amount = betrag
        existing.interval = iv
        db.add(existing)
    else:
        db.add(StandardBudget(category_id=category_id, amount=betrag, interval=iv))
    db.commit()
    return _render_root(request, db, month_start, user.preferred_theme)


@router.post("/autofill", response_class=HTMLResponse)
def autofill_budget(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    month: Annotated[str, Form()],
) -> Response:
    """Füllt leere Standard-Soll-Werte automatisch aus dem Median der bisherigen
    Ausgaben (überschreibt manuell gesetzte Werte nicht)."""
    month_start = _parse_month(month)
    # Der Zähler wurde bisher verworfen — der Knopf lief also scheinbar ins
    # Leere, gerade wenn es nichts mehr zu füllen gab.
    anzahl = autofill_standard_budgets(db, month_start)
    info = (f"{anzahl} Soll-Werte aus dem Median gefüllt." if anzahl
            else "Nichts zu füllen — alle Kategorien haben bereits ein Soll "
                 "oder keine Ausgaben-Historie.")
    return _render_root(request, db, month_start, user.preferred_theme, info=info)


@router.post("/set", response_class=HTMLResponse)
def set_budget(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    category_id: Annotated[int, Form()],
    month: Annotated[str, Form()],
    soll: Annotated[str, Form()],
) -> Response:
    """Setzt (oder löscht) einen **Monats-Override** (überschreibt den Standard)."""
    month_start = _parse_month(month)
    try:
        amount = parse_amount(soll)
    except InvalidOperation:
        return _render_root(request, db, month_start, user.preferred_theme)
    if db.get(Category, category_id) is None:  # stale Ansicht, s. set_standard_budget
        return _render_root(request, db, month_start, user.preferred_theme)

    existing = db.scalar(
        select(Budget).where(Budget.category_id == category_id, Budget.month == month_start)
    )
    if amount <= 0:
        if existing is not None:
            db.delete(existing)
    elif existing is not None:
        existing.planned_amount = amount
        existing.is_auto_calculated = False
        db.add(existing)
    else:
        db.add(Budget(category_id=category_id, month=month_start,
                      planned_amount=amount, is_auto_calculated=False))
    db.commit()
    return _render_root(request, db, month_start, user.preferred_theme)
