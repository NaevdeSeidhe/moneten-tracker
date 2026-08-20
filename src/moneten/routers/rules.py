"""Kategorisierungs-Regeln verwalten + rückwirkend anwenden.

UI-Muster wie sonst: ein Container ``#rules-root``, der bei jeder Aktion neu
gerendert wird.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import (
    Category,
    CategoryRule,
    ManagementType,
    Transaction,
    User,
    enthaelt,
    not_transfer,
)
from moneten.db.session import get_db
from moneten.services.categorization import (
    apply_rules,
    learn_from_transaction,
    load_active_rules,
    match_category,
    transfer_category_ids,
    uncategorized_groups,
)
from moneten.services.hygiene import hygiene_befunde
from moneten.templating import templates

router = APIRouter(tags=["rules"])


def _category_groups(db: Session) -> list[tuple[str, list[tuple[int, str]]]]:
    """Kategorien als optgroup-Struktur fürs Auswahlfeld."""
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


def _uncategorized_count(db: Session) -> int:
    """Anzahl Buchungen ohne Kategorie (echte Ausgaben/Einnahmen, keine Transfers)."""
    return db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.category_id.is_(None),
            Transaction.is_split.is_(False),  # aufgeteilte Buchungen gelten als zugeordnet
            not_transfer(),
        )
    ) or 0


def _categorizable_total(db: Session) -> int:
    """Gesamtzahl kategorisierbarer Buchungen (Nenner für den Fortschritt):
    echte Ausgaben/Einnahmen, keine Transfers; aufgeteilte gelten als zugeordnet."""
    return db.scalar(
        select(func.count(Transaction.id)).where(Transaction.is_split.is_(False), not_transfer())
    ) or 0


def _root_context(db: Session, *, message: str | None = None) -> dict:
    """Gemeinsamer Template-Kontext für #rules-root (Partial + Vollseite)."""
    rules = list(
        db.scalars(select(CategoryRule).order_by(CategoryRule.sort_order, CategoryRule.id))
    )
    cat_names = {c.id: c.name for c in db.scalars(select(Category))}
    return {
        "rules": rules,
        "cat_names": cat_names,
        "category_groups": _category_groups(db),
        "uncategorized": _uncategorized_count(db),
        "cat_total": _categorizable_total(db),
        "inbox_groups": uncategorized_groups(db),
        # Aufräum-Befunde: was tot ist, ohne dass es einen Fehler verursacht.
        "hygiene": hygiene_befunde(db, heute_lokal()),
        "message": message,
    }


def _undo_trigger(
    resp: Response, ids: list[int], message: str, *, rule_id: int | None = None
) -> Response:
    """Hängt einen HX-Trigger an, der client-seitig einen Toast mit „Rückgängig"
    zeigt. Die Undo-Aktion ruft /rules/undo mit den betroffenen Buchungs-IDs auf
    und setzt deren Kategorie wieder auf „offen" (genau gegen den Vorfall, dass
    eine Sammel-Zuordnung Buchungen unerwartet umkategorisiert hat). Bei einer
    Gruppen-Zuordnung wird zusätzlich die dabei **gelernte** Regel (``rule_id``)
    mit zurückgenommen, damit das Undo vollständig ist."""
    values = {"tx_ids": ",".join(str(i) for i in ids)}
    if rule_id is not None:
        values["rule_id"] = str(rule_id)
    resp.headers["HX-Trigger"] = json.dumps({
        "moneten:toast": {
            "message": message,
            "undo": {
                "url": "/rules/undo", "target": "#rules-root", "swap": "innerHTML",
                "values": values,
            },
        }
    })
    return resp


def _render_root(request: Request, db: Session, *, message: str | None = None) -> Response:
    return templates.TemplateResponse(
        request, "partials/rules_root.html", _root_context(db, message=message)
    )


def _tx_dict(tx: Transaction) -> dict:
    return {"id": tx.id, "date": tx.date, "desc": tx.description or "(ohne Beschreibung)", "amount": tx.amount}


def _render_inbox_tx(
    request: Request, db: Session, *, tx: Transaction, mode: str,
    suggested: int | None = None, done_cat: str | None = None,
) -> Response:
    """Rendert NUR eine einzelne Inbox-Zeile (leicht/Picker/erledigt) — für schnelle,
    zeilengenaue HTMX-Swaps statt die ganze Seite neu zu bauen."""
    ctx: dict = {"tx": _tx_dict(tx), "mode": mode, "suggested": suggested, "done_cat": done_cat}
    if mode == "pick":
        ctx["category_groups"] = _category_groups(db)
    return templates.TemplateResponse(request, "partials/inbox_tx.html", ctx)


@router.get("", response_class=HTMLResponse)
def rules_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {"user": user, "active_tab": "rules", **_root_context(db)},
    )


@router.post("", response_class=HTMLResponse)
def add_rule(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    keyword: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
) -> Response:
    keyword = keyword.strip()
    cat = db.get(Category, int(category_id)) if category_id else None
    if not keyword or cat is None:
        return _render_root(request, db, message="Bitte Stichwort und Kategorie wählen.")
    max_order = db.scalar(select(func.max(CategoryRule.sort_order))) or 100
    db.add(CategoryRule(keyword=keyword, category_id=cat.id, sort_order=max_order + 10))
    db.commit()
    return _render_root(request, db)


@router.post("/bulk", response_class=HTMLResponse)
def add_rules_bulk(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    text: Annotated[str, Form()],
) -> Response:
    """Mehrere Regeln auf einmal: pro Zeile ``stichwort = Kategoriename``.

    Leerzeilen und ``#``-Kommentare werden ignoriert. Bereits vorhandene
    Stichwörter werden übersprungen; unbekannte Kategorienamen gemeldet.
    """
    cats = {c.name.lower(): c.id for c in db.scalars(select(Category))}
    existing = {(r.keyword or "").lower() for r in db.scalars(select(CategoryRule))}
    order = (db.scalar(select(func.max(CategoryRule.sort_order))) or 100)

    added = skipped = 0
    unknown: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        kw, _, catname = line.partition("=")
        kw, catname = kw.strip(), catname.strip()
        if not kw or not catname:
            continue
        cid = cats.get(catname.lower())
        if cid is None:
            unknown.append(catname)
            continue
        if kw.lower() in existing:
            skipped += 1
            continue
        order += 10
        db.add(CategoryRule(keyword=kw, category_id=cid, sort_order=order))
        existing.add(kw.lower())
        added += 1
    db.commit()

    msg = f"{added} Regel(n) hinzugefügt"
    if skipped:
        msg += f", {skipped} bereits vorhanden"
    if unknown:
        msg += f". Unbekannte Kategorien (nicht angelegt): {', '.join(sorted(set(unknown)))}"
    return _render_root(request, db, message=msg)


@router.post("/{rule_id:int}/delete", response_class=HTMLResponse)
def delete_rule(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    rule_id: int,
) -> Response:
    rule = db.get(CategoryRule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    return _render_root(request, db)


@router.post("/assign-group", response_class=HTMLResponse)
def assign_group(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    keyword: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
) -> Response:
    """Schnell-Zuordnen: ordnet eine ganze Händler-Gruppe einer Kategorie zu und
    merkt sich die Regel für die Zukunft (eine Aktion statt vieler Einzel-Klicks).

    Nutzt :func:`learn_from_transaction`: legt eine Lern-Regel an und ordnet alle
    noch unkategorisierten Buchungen mit diesem Stichwort derselben Kategorie zu.
    """
    keyword = keyword.strip()
    cat = db.get(Category, int(category_id)) if category_id else None
    if not keyword or cat is None:
        return _render_root(request, db, message="Bitte eine Kategorie für die Gruppe wählen.")
    # Betroffene Buchungen VOR der Zuordnung erfassen (gleiche Bedingung wie
    # learn_from_transaction) — für den korrekten Zähler UND fürs Undo.
    kw = keyword.lower()
    affected = list(
        db.scalars(
            select(Transaction.id).where(
                Transaction.category_id.is_(None),
                not_transfer(),
                enthaelt(Transaction.description, kw),
            )
        )
    )
    created, _applied = learn_from_transaction(db, keyword=keyword, category_id=cat.id)
    learned = db.scalar(
        select(CategoryRule).where(
            CategoryRule.is_active.is_(True), func.lower(CategoryRule.keyword) == kw
        )
    )
    msg = f"„{keyword}“ → {cat.name}: {len(affected)} Buchung(en) zugeordnet, Regel gemerkt."
    resp = _render_root(request, db, message=msg)
    # Undo-Toast: macht die Gruppen-Zuordnung rückgängig (Buchungen wieder offen +
    # die NEU gelernte Regel löschen). So ist ein Fehlklick auf „Alle (N)" in einem
    # Tap reparierbar — auch wenn versehentlich abgewählte Buchungen mit erfasst wurden.
    return _undo_trigger(resp, affected, msg, rule_id=(learned.id if created and learned else None))


@router.get("/row/{tx_id:int}", response_class=HTMLResponse)
def inbox_row(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    light: int = 0,
) -> Response:
    """Liefert eine einzelne Inbox-Zeile: leichte Ansicht (``light=1``) oder mit
    Kategorie-Picker (Standard). Der Picker wird so erst auf Klick geladen → die
    Liste bleibt leicht und schnell."""
    tx = db.get(Transaction, tx_id)
    if tx is None:
        # Buchung zwischenzeitlich weg → ganze Inbox neu laden (Retarget) + Hinweis,
        # statt nacktem 404, das HTMX in der Zeile nicht swappt (tote UI).
        resp = _render_root(request, db, message="Diese Buchung ist nicht mehr offen.")
        resp.headers["HX-Retarget"] = "#rules-root"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    if light:
        return _render_inbox_tx(request, db, tx=tx, mode="light")
    suggested = match_category(load_active_rules(db), tx.description)
    return _render_inbox_tx(request, db, tx=tx, mode="pick", suggested=suggested)


@router.post("/assign-one", response_class=HTMLResponse)
def assign_one(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    transaction_id: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
) -> Response:
    """Ordnet EINE einzelne Buchung einer Kategorie zu (ohne Lern-Regel). Rendert
    NUR diese Zeile neu (grün, erledigt) → schnell + die Gruppe bleibt offen."""
    tx = db.get(Transaction, int(transaction_id)) if transaction_id else None
    if tx is None:
        # Buchung zwischenzeitlich weg → ganze Inbox neu laden (Retarget) + Hinweis,
        # statt nacktem 404, das HTMX in der Zeile nicht swappt (tote UI).
        resp = _render_root(request, db, message="Diese Buchung ist nicht mehr offen.")
        resp.headers["HX-Retarget"] = "#rules-root"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    cat = db.get(Category, int(category_id)) if category_id else None
    if cat is None:
        # Keine Kategorie gewählt → Picker erneut zeigen (Vorschlag vorausgewählt).
        suggested = match_category(load_active_rules(db), tx.description)
        return _render_inbox_tx(request, db, tx=tx, mode="pick", suggested=suggested)
    tx.category_id = cat.id
    if cat.id in transfer_category_ids(db):
        tx.management_type = ManagementType.TRANSFER
    db.commit()
    resp = _render_inbox_tx(request, db, tx=tx, mode="done", done_cat=cat.name)
    return _undo_trigger(resp, [tx.id], f"1 Buchung → {cat.name} zugeordnet.")


@router.post("/assign-many", response_class=HTMLResponse)
def assign_many(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    transaction_id: Annotated[list[str], Form()] = [],  # noqa: B006 (FastAPI-Form-Liste)
    category_id: Annotated[str, Form()] = "",
) -> Response:
    """Ordnet MEHRERE ausgewählte Buchungen auf einmal einer Kategorie zu
    (Bulk-Editing aus der Inbox: filtern → markieren → in einem Rutsch zuordnen).

    Bewusst OHNE Lern-Regel — eine manuelle Sammel-Zuweisung soll keine
    Auto-Regel anlegen (die Auswahl kann gemischte Händler enthalten)."""
    cat = db.get(Category, int(category_id)) if category_id else None
    ids = [int(t) for t in transaction_id if t.strip().isdigit()]
    if cat is None or not ids:
        return _render_root(request, db, message="Bitte Buchungen markieren und eine Kategorie wählen.")
    is_transfer = cat.id in transfer_category_ids(db)
    changed: list[int] = []
    for tx in db.scalars(select(Transaction).where(Transaction.id.in_(ids))):
        tx.category_id = cat.id
        if is_transfer:
            tx.management_type = ManagementType.TRANSFER
        changed.append(tx.id)
    db.commit()
    resp = _render_root(request, db, message=f"{len(changed)} Buchung(en) → {cat.name} zugeordnet.")
    return _undo_trigger(resp, changed, f"{len(changed)} Buchung(en) → {cat.name} zugeordnet.")


@router.post("/apply", response_class=HTMLResponse)
def apply_now(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Wendet alle Regeln auf noch nicht kategorisierte Buchungen an."""
    n = apply_rules(db, only_uncategorized=True)
    return _render_root(request, db, message=f"{n} Buchung(en) neu zugeordnet.")


@router.post("/undo", response_class=HTMLResponse)
def undo_assign(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_ids: Annotated[str, Form()] = "",
    rule_id: Annotated[str, Form()] = "",
) -> Response:
    """Macht eine Sammel-/Einzel-/Gruppen-Zuordnung rückgängig: setzt die betroffenen
    Buchungen wieder auf „offen" (Kategorie zurück) und entfernt — falls bei einer
    Gruppen-Zuordnung eine Regel **neu gelernt** wurde — auch diese wieder. Gegen
    versehentliche Zuordnungen — sicherer als nur eine Nachfrage."""
    ids = [int(t) for t in tx_ids.split(",") if t.strip().isdigit()]
    n = 0
    for tx in db.scalars(select(Transaction).where(Transaction.id.in_(ids))):
        tx.category_id = None
        if tx.management_type == ManagementType.TRANSFER:
            tx.management_type = None
        n += 1
    rule_removed = False
    if rule_id.strip().isdigit():
        rule = db.get(CategoryRule, int(rule_id))
        if rule is not None:
            db.delete(rule)
            rule_removed = True
    if n or rule_removed:
        db.commit()
    msg = f"Rückgängig: {n} Buchung(en) wieder offen"
    msg += " + gelernte Regel entfernt." if rule_removed else "."
    return _render_root(request, db, message=msg)
