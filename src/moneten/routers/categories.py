"""Kategorie-Verwaltung: eigene Kategorien anlegen, umbenennen, Icon/Farbe
setzen, Unterkategorien umhängen und archivieren.

UI-Muster wie sonst: ein Container ``#categories-root``, der bei jeder Aktion neu
gerendert wird. Zwei Ebenen: Top-Kategorien (``parent_id`` NULL) und deren
Unterkategorien. Buchungen hängen an der Unterkategorie — wird diese in eine
andere Top-Kategorie verschoben, ziehen alle Buchungen automatisch mit (Reports
aggregieren über die Eltern, die ID der Unterkategorie bleibt gleich).

Löschen ist nur erlaubt, wenn keine Buchungen und keine Unterkategorien dran
hängen — sonst Archivieren (kein Datenverlust).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.db.models import (
    Budget,
    Category,
    CategoryRule,
    ManagementType,
    ManualSubscription,
    MetricSeries,
    ReceiptItemRule,
    StandardBudget,
    Transaction,
    TransactionSplit,
    User,
)
from moneten.db.session import get_db
from moneten.icons import PICKER_ICONS
from moneten.palette import CHART_COLORS
from moneten.templating import templates

router = APIRouter(tags=["categories"])

# Freundliche „Art" für NEUE Top-Kategorien → management_type-Code.
ART_CHOICES: list[tuple[str, str]] = [
    ("B", "Ausgabe"),
    ("E", "Einnahme"),
    ("S", "Sparen / Rücklage"),
    ("T", "Transfer / Umbuchung"),
]
_ART_LABELS = {code: label for code, label in ART_CHOICES}


def _tx_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Transaction.category_id, func.count(Transaction.id)).group_by(Transaction.category_id)
    ).all()
    return {cid: n for cid, n in rows if cid is not None}


def _tree(db: Session) -> list[dict]:
    """Top-Kategorien mit ihren Unterkategorien + Buchungs-Anzahl je Kategorie."""
    cats = list(db.scalars(select(Category).order_by(Category.sort_order, Category.name)))
    counts = _tx_counts(db)
    by_parent: dict[int, list[Category]] = {}
    tops: list[Category] = []
    for c in cats:
        if c.parent_id is None:
            tops.append(c)
        else:
            by_parent.setdefault(c.parent_id, []).append(c)

    def _entry(c: Category) -> dict:
        return {
            "id": c.id, "name": c.name, "icon": c.icon or "tag", "color": c.color,
            "is_archived": c.is_archived, "tx_count": counts.get(c.id, 0),
            "mgmt": _ART_LABELS.get(c.management_type.value if c.management_type else "", ""),
        }

    out = []
    for top in tops:
        subs = [_entry(s) for s in by_parent.get(top.id, [])]
        e = _entry(top)
        e["subs"] = subs
        e["sub_tx_total"] = sum(s["tx_count"] for s in subs)
        out.append(e)
    return out


def _top_choices(db: Session) -> list[tuple[int, str]]:
    return [
        (c.id, c.name)
        for c in db.scalars(
            select(Category).where(Category.parent_id.is_(None), Category.is_archived.is_(False))
            .order_by(Category.sort_order, Category.name)
        )
    ]


def _root_context(db: Session, *, form_mode: str = "none", edit_cat: Category | None = None,
                  message: str | None = None, error: str | None = None,
                  form_values: dict | None = None) -> dict:
    return {
        "tree": _tree(db),
        "top_choices": _top_choices(db),
        "icons": PICKER_ICONS,
        # Die Auswahl kommt aus der Palette, nicht aus einer Liste in der Vorlage.
        # Dort stand eine alte Kopie, in der #D97757 und #C26851 als Muster
        # NEBENEINANDER lagen — dE 8.61, im Bild nicht auseinanderzuhalten. Genau
        # dieses Paar war in ``moneten.palette`` längst korrigiert; die Kopie hat die
        # Korrektur nicht mitbekommen. Eine Kategorie-Farbe wird als Hex gespeichert
        # (sie gilt über alle Skins), darum die Hex-Liste und nicht ``var(--chart-N)``.
        "farb_auswahl": CHART_COLORS,
        "art_choices": ART_CHOICES,
        "form_mode": form_mode,
        "edit_cat": edit_cat,
        "message": message,
        "error": error,
        # Rohwerte nach einem Validierungsfehler — sonst ist die Maske leer.
        "form_values": form_values or {},
    }


def _render_root(request: Request, db: Session, **kw) -> Response:
    return templates.TemplateResponse(request, "partials/categories_root.html", _root_context(db, **kw))


@router.get("", response_class=HTMLResponse)
def categories_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    form: str = "none",
    id: int | None = None,
) -> Response:
    edit_cat = db.get(Category, id) if (form == "edit" and id is not None) else None
    if edit_cat is None and form == "edit":
        form = "none"
    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db, form_mode=form, edit_cat=edit_cat)
    ctx = {"user": user, "active_tab": "settings", **_root_context(db, form_mode=form, edit_cat=edit_cat)}
    return templates.TemplateResponse(request, "categories.html", ctx)


def _apply(db: Session, cat: Category, *, name: str, parent_id: str, icon: str,
           color: str, art: str) -> str | None:
    """Setzt die Felder; gibt Fehlertext oder None. Erzwingt max. 2 Ebenen."""
    name = name.strip()
    if not name:
        return "Bitte einen Namen angeben."
    parent = db.get(Category, int(parent_id)) if parent_id else None
    if parent is not None:
        if parent.id == cat.id:
            return "Eine Kategorie kann nicht ihre eigene Oberkategorie sein."
        if parent.parent_id is not None:
            return "Die Oberkategorie muss selbst eine Top-Kategorie sein (max. 2 Ebenen)."
        # Bestehende Kategorie mit eigenen Unterkategorien darf nicht selbst eine
        # werden (nur prüfen, wenn die Kategorie schon existiert — sonst würde
        # ``parent_id == None`` bei einer neuen Kategorie zu ``IS NULL`` und alle
        # Top-Kategorien zählen).
        if cat.id is not None:
            has_children = db.scalar(
                select(func.count(Category.id)).where(Category.parent_id == cat.id)
            )
            if has_children:
                return "Diese Kategorie hat Unterkategorien und kann nicht selbst eine werden."
    cat.name = name
    cat.parent_id = parent.id if parent else None
    cat.icon = (icon or "").strip() or None
    color = (color or "").strip()
    cat.color = color if color.startswith("#") else None
    if parent is not None:
        cat.management_type = parent.management_type  # Unterkategorie erbt die Art
    elif art in _ART_LABELS:
        cat.management_type = ManagementType(art)
    elif cat.management_type is None:
        cat.management_type = ManagementType.BARGELD
    return None


@router.post("", response_class=HTMLResponse)
def create_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    parent_id: Annotated[str, Form()] = "",
    icon: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    art: Annotated[str, Form()] = "B",
) -> Response:
    cat = Category(name="", management_type=ManagementType.BARGELD)
    error = _apply(db, cat, name=name, parent_id=parent_id, icon=icon, color=color, art=art)
    if error:
        return _render_root(request, db, form_mode="new", error=error,
                            form_values={"name": name, "parent_id": parent_id,
                                         "icon": icon, "color": color, "art": art})
    max_order = db.scalar(select(func.max(Category.sort_order))) or 0
    cat.sort_order = max_order + 10
    db.add(cat)
    db.commit()
    return _render_root(request, db, message=f"Kategorie „{cat.name}“ angelegt.")


@router.post("/{cat_id:int}", response_class=HTMLResponse)
def update_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    cat_id: int,
    name: Annotated[str, Form()] = "",
    parent_id: Annotated[str, Form()] = "",
    icon: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    art: Annotated[str, Form()] = "",
) -> Response:
    cat = db.get(Category, cat_id)
    if cat is None:
        return _render_root(request, db, error="Kategorie nicht gefunden.")
    error = _apply(db, cat, name=name, parent_id=parent_id, icon=icon, color=color, art=art)
    if error:
        return _render_root(request, db, form_mode="edit", edit_cat=cat, error=error)
    db.add(cat)
    db.commit()
    return _render_root(request, db, message=f"„{cat.name}“ gespeichert.")


@router.post("/{cat_id:int}/archive", response_class=HTMLResponse)
def archive_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    cat_id: int,
) -> Response:
    cat = db.get(Category, cat_id)
    if cat is not None:
        cat.is_archived = True
        db.commit()
    return _render_root(request, db, message="Kategorie archiviert.")


@router.post("/{cat_id:int}/unarchive", response_class=HTMLResponse)
def unarchive_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    cat_id: int,
) -> Response:
    cat = db.get(Category, cat_id)
    if cat is not None:
        cat.is_archived = False
        db.commit()
    return _render_root(request, db, message="Kategorie wieder aktiviert.")


@router.post("/{cat_id:int}/delete", response_class=HTMLResponse)
def delete_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    cat_id: int,
) -> Response:
    """Löschen nur, wenn nichts dranhängt: keine Buchungen, Unterkategorien,
    Aufteilungs-Anteile, Budgets, (Lern-)Regeln, Abos/Fixposten oder
    Verlaufsreihen — sonst Archivieren.

    Wichtig: Aufteilungs-Anteile zählen mit! Eine aufgeteilte Buchung hat
    ``category_id=None`` — eine reine Split-Kategorie zeigte „0 Buchungen" und
    liess sich löschen, dabei wurden per FK die Split-Anteile still auf NULL
    gesetzt (Budget/Vergleich rückwirkend verfälscht) und Budgets/Regeln
    kaskadiert gelöscht."""
    cat = db.get(Category, cat_id)
    if cat is None:
        return _render_root(request, db, error="Kategorie nicht gefunden.")
    tx_n = db.scalar(select(func.count(Transaction.id)).where(Transaction.category_id == cat_id)) or 0
    child_n = db.scalar(select(func.count(Category.id)).where(Category.parent_id == cat_id)) or 0
    split_n = db.scalar(
        select(func.count(TransactionSplit.id)).where(TransactionSplit.category_id == cat_id)
    ) or 0
    ref_n = sum(
        db.scalar(select(func.count(model.id)).where(model.category_id == cat_id)) or 0
        for model in (Budget, StandardBudget, CategoryRule, ReceiptItemRule)
    )
    # **Abos und Verlaufsreihen zählen ebenfalls mit.** Beide zeigen mit einem
    # nullbaren ``category_id`` auf die Kategorie, also mit ``ON DELETE SET NULL``:
    # Das Löschen wäre nicht gescheitert, es hätte die Verknüpfung still gelöst.
    # Bei einer Verlaufsreihe ist das der stillste Fall der App — der Soll/Ist-
    # Abgleich vergleicht dann gegen nichts und meldet weiter „keine Abweichung".
    #
    # Getrennt gezählt, weil die Meldung sagen muss, WO man nachsehen muss;
    # „Budget-/Regel-Einträge" hätte an der falschen Stelle suchen lassen.
    abo_n = db.scalar(
        select(func.count(ManualSubscription.id)).where(ManualSubscription.category_id == cat_id)
    ) or 0
    reihe_n = db.scalar(
        select(func.count(MetricSeries.id)).where(MetricSeries.category_id == cat_id)
    ) or 0
    if tx_n or child_n or split_n or ref_n or abo_n or reihe_n:
        parts = []
        if tx_n:
            parts.append(f"{tx_n} Buchung(en)")
        if split_n:
            parts.append(f"{split_n} Aufteilungs-Anteil(e)")
        if child_n:
            parts.append(f"{child_n} Unterkategorie(n)")
        if ref_n:
            parts.append(f"{ref_n} Budget-/Regel-Einträge")
        if abo_n:
            parts.append(f"{abo_n} Abo/Fixposten" if abo_n == 1 else f"{abo_n} Abos/Fixposten")
        if reihe_n:
            parts.append(f"{reihe_n} Verlaufsreihe(n)")
        return _render_root(
            request, db, form_mode="edit", edit_cat=cat,
            error=f"„{cat.name}“ hat {', '.join(parts)} — bitte stattdessen archivieren.",
        )
    name = cat.name
    db.delete(cat)
    db.commit()
    return _render_root(request, db, message=f"Kategorie „{name}“ gelöscht.")
