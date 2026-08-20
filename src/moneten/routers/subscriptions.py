"""Abos-Seite: automatisch erkannte **und** manuell gepflegte Abos.

* **Erkannt** (read-only, aus den Buchungen) — falsch Erkanntes lässt sich per
  „ist kein Abo" ausblenden oder per „übernehmen & anpassen" in ein manuelles
  Abo überführen.
* **Manuell** — anlegen, bearbeiten, löschen.

UI-Muster wie sonst: ein Container ``#subscriptions-root``, der bei jeder Aktion
per HTMX neu gerendert wird.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.db.models import (
    Account,
    BudgetInterval,
    Category,
    DismissedMerchant,
    ManualSubscription,
    User,
)
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.palette import color_at, icon_color_at
from moneten.services.cancel_effect import kuendigungs_effekt
from moneten.services.median_budget import monthly_equivalent
from moneten.services.subscriptions import (
    connected_counts,
    detect_subscriptions,
    match_transactions,
)
from moneten.templating import templates

# Gültige Arten eines manuellen Eintrags: echtes Abo vs. wiederkehrende Zahlung.
_KINDS = ("abo", "fix")

router = APIRouter(tags=["subscriptions"])


def _category_groups(db: Session) -> list[tuple[str, list[tuple[int, str]]]]:
    tops = db.scalars(
        select(Category).where(Category.parent_id.is_(None), Category.is_archived.is_(False)).order_by(Category.sort_order)
    ).all()
    groups = []
    for top in tops:
        subs = db.scalars(
            select(Category).where(Category.parent_id == top.id, Category.is_archived.is_(False)).order_by(Category.sort_order)
        ).all()
        if subs:
            groups.append((top.name, [(s.id, s.name, s.icon) for s in subs]))
    return groups


def _detected_kind(s) -> str:
    """Erkannte wiederkehrende Zahlung in „abo" oder „fix" einordnen — anhand des
    is_subscription-Flags ihrer Kategorie (Streaming/Software/… = Abo)."""
    return "abo" if (s.category is not None and s.category.is_subscription) else "fix"


def _segments(items: list[tuple[str, Decimal, str | None]], theme: str | None = None) -> list[dict]:
    """Aufteilungs-Segmente (fürs split_bar-Makro) aus (Name, Monatsbetrag, Icon).

    Grösste zuerst; Farben (theme-bewusst) + lesbare Icon-Farbe + Prozent-Anteil ergänzt."""
    rows = sorted([it for it in items if it[1] and it[1] > 0], key=lambda it: it[1], reverse=True)
    total = sum((it[1] for it in rows), Decimal("0"))
    segs = []
    for i, (name, monthly, ic) in enumerate(rows):
        color = color_at(i, theme)
        segs.append({
            "label": name, "value": monthly, "icon": ic or "repeat",
            "color": color, "icon_color": icon_color_at(i),
            "pct": round(float(monthly / total * 100), 1) if total > 0 else 0,
        })
    return segs


def _base_context(
    db: Session, *, form_mode: str = "none", edit_sub: ManualSubscription | None = None,
    error: str | None = None, prefill: dict | None = None, theme: str | None = None,
    form_values: dict | None = None,
) -> dict:
    manual = list(db.scalars(
        select(ManualSubscription).where(ManualSubscription.is_active.is_(True)).order_by(ManualSubscription.name)
    ))
    # Mit Bankbuchungen verbundene Händler nicht zusätzlich automatisch erkennen.
    linked_keys = {m.match_keyword for m in manual if m.match_keyword}
    detected = detect_subscriptions(db, extra_skip=linked_keys)
    conn = connected_counts(db, linked_keys)

    # In zwei Eimer trennen: echte Abos vs. wiederkehrende (Fix-)Zahlungen.
    # „Vermutlich gekündigt" (stale) wird getrennt geführt und NICHT mitgezählt.
    buckets: dict[str, dict] = {}
    for kind in _KINDS:
        man = [m for m in manual if (m.kind or "abo") == kind]
        det_all = [s for s in detected if _detected_kind(s) == kind]
        det = [s for s in det_all if not s.stale]
        det_stale = [s for s in det_all if s.stale]
        man_rows = [{
            "m": m, "monthly": monthly_equivalent(m.amount, m.interval),
            "connected": conn.get(m.match_keyword, 0) if m.match_keyword else 0,
            # Was Kündigen brächte — als Bezug zu einem echten Sparziel, nicht
            # als abstrakte Jahreszahl.
            "kuendigung": kuendigungs_effekt(db, m),
        } for m in man]
        seg_items = (
            [(m.name, monthly_equivalent(m.amount, m.interval), m.category.icon if m.category else None) for m in man]
            + [(s.name or "?", s.monthly, s.category.icon if s.category else None) for s in det]
        )
        monthly_total = (
            sum((r["monthly"] for r in man_rows), Decimal("0"))
            + sum((s.monthly for s in det), Decimal("0"))
        )
        buckets[kind] = {
            "manual_rows": man_rows,
            "detected": det,
            "detected_stale": det_stale,
            "segments": _segments(seg_items, theme),
            "monthly": monthly_total,
            "yearly": (monthly_total * 12).quantize(Decimal("0.01")),
            "count": len(man_rows) + len(det),
        }

    grand_monthly = buckets["abo"]["monthly"] + buckets["fix"]["monthly"]
    return {
        "buckets": buckets,
        "summary": {
            "count": buckets["abo"]["count"] + buckets["fix"]["count"],
            "monthly": grand_monthly,
            "yearly": (grand_monthly * 12).quantize(Decimal("0.01")),
        },
        "accounts": list(db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.sort_order))),
        "category_groups": _category_groups(db),
        "dismissed": list(db.scalars(select(DismissedMerchant).order_by(DismissedMerchant.merchant_key))),
        "form_mode": form_mode,
        "edit_sub": edit_sub,
        "prefill": prefill,
        "form_values": form_values or {},
        "error": error,
    }


def _render_root(request: Request, db: Session, *, status_code: int = 200, **kw) -> Response:
    return templates.TemplateResponse(
        request, "partials/subscriptions_root.html", _base_context(db, **kw), status_code=status_code
    )


@router.get("", response_class=HTMLResponse)
def subscriptions_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    form: str = "none",
    id: int | None = None,
    match_q: str = "",
) -> Response:
    """Abos-Seite (erkannt + manuell). ``form=edit&id=..`` öffnet das Bearbeiten.

    ``match_q`` sucht passende Bankbuchungen und öffnet das Formular vorbefüllt
    (Name + Median-Betrag + verbundener Händler-Schlüssel)."""
    edit_sub = db.get(ManualSubscription, id) if (form == "edit" and id is not None) else None
    prefill = None
    if match_q.strip():
        result = match_transactions(db, match_q)
        prefill = result if result else {"none": True}
        prefill["query"] = match_q.strip()
        form = "new"
    theme = user.preferred_theme  # Split-Bar-Farben ans aktive Theme koppeln
    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db, form_mode=form, edit_sub=edit_sub, prefill=prefill, theme=theme)
    ctx = _base_context(db, form_mode=form, edit_sub=edit_sub, prefill=prefill, theme=theme)
    ctx |= {"user": user, "active_tab": "subscriptions"}
    return templates.TemplateResponse(request, "subscriptions.html", ctx)


def _parse_sub_form(amount: str, interval: str) -> tuple[Decimal | None, BudgetInterval]:
    iv = BudgetInterval.JAEHRLICH if interval == "jaehrlich" else BudgetInterval.MONATLICH
    try:
        betrag = parse_amount(amount)
    except InvalidOperation:
        return None, iv
    return betrag, iv


@router.post("", response_class=HTMLResponse)
def create_subscription(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    interval: Annotated[str, Form()] = "monatlich",
    kind: Annotated[str, Form()] = "abo",
    category_id: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    match_keyword: Annotated[str, Form()] = "",
) -> Response:
    """Legt einen manuellen Eintrag an (Abo oder wiederkehrende Zahlung)."""
    betrag, iv = _parse_sub_form(amount, interval)
    if not name.strip() or betrag is None or betrag <= 0:
        _roh = {"name": name, "amount": amount, "interval": interval, "kind": kind,
                "category_id": category_id, "account_id": account_id, "notes": notes}
        return _render_root(request, db, form_mode="new", theme=user.preferred_theme,
                            error="Bitte Name und einen Betrag grösser als 0 angeben.",
                            status_code=400, form_values=_roh)
    db.add(ManualSubscription(
        name=name.strip(), amount=betrag, interval=iv,
        kind=kind if kind in _KINDS else "abo",
        category_id=int(category_id) if category_id else None,
        account_id=int(account_id) if account_id else None,
        notes=notes.strip() or None,
        match_keyword=match_keyword.strip() or None,
    ))
    db.commit()
    return _render_root(request, db, theme=user.preferred_theme)


@router.post("/{sub_id:int}/update", response_class=HTMLResponse)
def update_subscription(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    sub_id: int,
    name: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    interval: Annotated[str, Form()] = "monatlich",
    kind: Annotated[str, Form()] = "abo",
    category_id: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    match_keyword: Annotated[str, Form()] = "",
) -> Response:
    """Aktualisiert einen manuellen Eintrag."""
    m = db.get(ManualSubscription, sub_id)
    if m is None:
        return _render_root(request, db, theme=user.preferred_theme, error="Eintrag nicht gefunden.", status_code=404)
    betrag, iv = _parse_sub_form(amount, interval)
    if not name.strip() or betrag is None or betrag <= 0:
        return _render_root(request, db, form_mode="edit", edit_sub=m, theme=user.preferred_theme,
                            error="Bitte Name und einen Betrag grösser als 0 angeben.", status_code=400)
    m.name = name.strip()
    m.amount = betrag
    m.interval = iv
    m.kind = kind if kind in _KINDS else "abo"
    m.category_id = int(category_id) if category_id else None
    m.account_id = int(account_id) if account_id else None
    m.notes = notes.strip() or None
    m.match_keyword = match_keyword.strip() or None
    db.add(m)
    db.commit()
    return _render_root(request, db, theme=user.preferred_theme)


@router.post("/{sub_id:int}/delete", response_class=HTMLResponse)
def delete_subscription(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    sub_id: int,
) -> Response:
    """Löscht ein manuelles Abo."""
    m = db.get(ManualSubscription, sub_id)
    if m is not None:
        db.delete(m)
        db.commit()
    return _render_root(request, db, theme=user.preferred_theme)


@router.post("/dismiss", response_class=HTMLResponse)
def dismiss_detected(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    merchant_key: Annotated[str, Form()],
) -> Response:
    """Blendet ein falsch erkanntes Abo künftig aus („ist kein Abo")."""
    key = merchant_key.strip()
    if key and db.scalar(select(DismissedMerchant).where(DismissedMerchant.merchant_key == key)) is None:
        db.add(DismissedMerchant(merchant_key=key))
        db.commit()
    return _render_root(request, db, theme=user.preferred_theme)


@router.post("/restore", response_class=HTMLResponse)
def restore_dismissed(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    merchant_key: Annotated[str, Form()],
) -> Response:
    """Hebt das Ausblenden eines Händlers wieder auf (wird künftig wieder erkannt)."""
    key = merchant_key.strip()
    dm = db.scalar(select(DismissedMerchant).where(DismissedMerchant.merchant_key == key))
    if dm is not None:
        db.delete(dm)
        db.commit()
    return _render_root(request, db, theme=user.preferred_theme)


@router.post("/adopt", response_class=HTMLResponse)
def adopt_detected(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    merchant_key: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    interval: Annotated[str, Form()] = "monatlich",
    category_id: Annotated[str, Form()] = "",
) -> Response:
    """Übernimmt ein erkanntes Abo als **manuelles** (editierbares) Abo.

    Der Händler-Schlüssel wandert als ``match_keyword`` mit. Das erledigt zwei
    Dinge auf einmal: der übernommene Eintrag zeigt weiter „N verbundene
    Buchungen", und die Auto-Erkennung überspringt den Händler (``extra_skip``),
    zählt ihn also nicht doppelt. Ein zusätzlicher ``DismissedMerchant`` — wie
    vorher — wäre falsch: der Nutzer hat den Händler nicht ausgeblendet, sondern
    übernommen, und nach dem Löschen des Eintrags soll er wieder erkannt werden."""
    betrag, iv = _parse_sub_form(amount, interval)
    if betrag is None or betrag <= 0:
        betrag = Decimal("0.01")
    cat = db.get(Category, int(category_id)) if category_id else None
    kind = "abo" if (cat is not None and cat.is_subscription) else "fix"
    db.add(ManualSubscription(
        name=(name.strip() or merchant_key.strip() or "Abo"), amount=betrag, interval=iv,
        kind=kind,
        category_id=cat.id if cat else None,
        match_keyword=merchant_key.strip() or None,
    ))
    db.commit()
    return _render_root(request, db, theme=user.preferred_theme)
