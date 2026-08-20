"""Mobile Quick-Add (PWA-Startseite am Handy).

Stark reduzierte Erfassung in wenigen Taps (Konzept Abschnitt 11):
* Status oben: heute ausgegeben + Rest im Monatsbudget.
* Grosser Betrag → Kategorie (Schnell-Pills aus der History oder Auswahl) → Konto → speichern.
* Letzte 5 Buchungen read-only.

Funktioniert auch am Desktop, ist aber fürs Handy optimiert (1-spaltig, grosse
Touch-Ziele). Die PWA (`manifest.start_url=/quick`) öffnet direkt hier.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import add_months, heute_lokal
from moneten.db.models import (
    Account,
    AccountType,
    Category,
    Transaction,
    User,
    not_transfer,
)
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.services.balances import recalc_account_balance
from moneten.services.budget_totals import monats_totale
from moneten.templating import templates

router = APIRouter(tags=["quick"])


def _category_groups(db: Session) -> list[tuple[str, list[tuple[int, str]]]]:
    """Kategorien als optgroup-Struktur fürs Auswahlfeld (nur Unterkategorien)."""
    tops = db.scalars(
        select(Category).where(Category.parent_id.is_(None), Category.is_archived.is_(False)).order_by(Category.sort_order)
    ).all()
    groups: list[tuple[str, list[tuple[int, str]]]] = []
    for top in tops:
        subs = db.scalars(
            select(Category).where(Category.parent_id == top.id, Category.is_archived.is_(False)).order_by(Category.sort_order)
        ).all()
        if subs:
            groups.append((top.name, [(s.id, s.name, s.icon) for s in subs]))
    return groups


def _quick_pills(db: Session, today: date) -> list[dict]:
    """Bis zu 4 häufigste Ausgabe-Kategorien der letzten 90 Tage, je mit dem
    am häufigsten genutzten Konto — als Ein-Tap-Vorauswahl."""
    since = add_months(today, -3)
    rows = db.execute(
        select(Transaction.category_id, Transaction.account_id, func.count(Transaction.id))
        .where(Transaction.amount < 0, Transaction.date >= since,
               Transaction.category_id.is_not(None), not_transfer())
        .group_by(Transaction.category_id, Transaction.account_id)
    ).all()
    # Pro Kategorie: Gesamtzahl + häufigstes Konto.
    by_cat: dict[int, dict] = {}
    for cat_id, acc_id, cnt in rows:
        e = by_cat.setdefault(cat_id, {"count": 0, "accs": {}})
        e["count"] += cnt
        e["accs"][acc_id] = e["accs"].get(acc_id, 0) + cnt
    top = sorted(by_cat.items(), key=lambda kv: kv[1]["count"], reverse=True)[:4]

    cats = {c.id: c for c in db.scalars(select(Category))}
    accs = {a.id: a for a in db.scalars(select(Account))}
    pills = []
    for cat_id, info in top:
        cat = cats.get(cat_id)
        if cat is None:
            continue
        best_acc_id = max(info["accs"], key=info["accs"].get) if info["accs"] else None
        acc = accs.get(best_acc_id) if best_acc_id else None
        pills.append({
            "category_id": cat_id, "category_name": cat.name, "icon": cat.icon,
            "account_id": acc.id if acc else "", "account_name": acc.name if acc else "",
        })
    return pills


def _status(db: Session, today: date) -> dict:
    """Heute ausgegeben + Rest im Monatsbudget (grobe, schnelle Kennzahlen)."""
    month_start = today.replace(day=1)
    today_spent = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.amount < 0, Transaction.date == today, not_transfer()
        )
    ) or 0
    # Genau dieselbe Rechnung wie die Leitzahl der Budget-Seite. Eine eigene
    # Kurzformel stand hier einmal — sie zählte unkategorisierte Buchungen voll
    # mit und zeigte darum am Handy einen systematisch tieferen Rest als
    # /budget. Zwei Antworten auf dieselbe Frage.
    gesamt = monats_totale(db, month_start)
    return {
        "today_spent": Decimal(str(today_spent)).copy_abs(),
        "month_soll": gesamt["soll"],
        "rest_month": gesamt["rest"] if gesamt["soll"] > 0 else None,
    }


def _context(db: Session, user, *, flash: str | None = None) -> dict:
    today = heute_lokal()
    accounts = list(db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.sort_order)))
    recent = list(db.scalars(
        select(Transaction).order_by(Transaction.date.desc(), Transaction.id.desc()).limit(5)
    ))
    # Standard-Konto fürs Handy: erstes Bargeld-Konto, sonst erstes Konto.
    default_acc = next((a for a in accounts if a.type == AccountType.CASH), accounts[0] if accounts else None)
    return {
        "user": user,
        "active_tab": "quick",
        "status": _status(db, today),
        "pills": _quick_pills(db, today),
        "recent": recent,
        "accounts": accounts,
        "category_groups": _category_groups(db),
        "default_account_id": default_acc.id if default_acc else "",
        "today": today.isoformat(),
        "flash": flash,
    }


@router.get("", response_class=HTMLResponse)
def quick_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Quick-Add-Startseite."""
    return templates.TemplateResponse(request, "quick.html", _context(db, user))


@router.post("", response_class=HTMLResponse)
def quick_create(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    amount: Annotated[str, Form()],
    account_id: Annotated[int, Form()],
    category_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "ausgabe",
    description: Annotated[str, Form()] = "",
) -> Response:
    """Erfasst eine Buchung in einem Schritt und zeigt die Seite mit Bestätigung neu."""
    account = db.get(Account, account_id)
    try:
        betrag = parse_amount(amount)
    except InvalidOperation:
        betrag = Decimal("0")
    if account is None or betrag <= 0:
        ctx = _context(db, user, flash=None)
        ctx["error"] = "Bitte gültigen Betrag und Konto angeben."
        # Rohwerte zurückgeben: sonst steht man vor einer leeren Maske und
        # tippt alles neu, obwohl nur ein Feld beanstandet wurde.
        ctx["form_values"] = {"amount": amount, "account_id": account_id,
                              "category_id": category_id, "kind": kind,
                              "description": description}
        return templates.TemplateResponse(request, "quick.html", ctx, status_code=400)

    cat = db.get(Category, int(category_id)) if category_id else None
    tx = Transaction(
        account_id=account_id,
        category_id=cat.id if cat else None,
        date=heute_lokal(),
        amount=betrag if kind == "einnahme" else -betrag,
        description=description.strip(),
        management_type=cat.management_type if cat else None,
    )
    db.add(tx)
    db.flush()
    recalc_account_balance(db, account_id)
    db.commit()
    return templates.TemplateResponse(request, "quick.html", _context(db, user, flash="✓ Buchung gespeichert."))
