"""Aufräum-Befunde: was im System tot ist, ohne dass es auffällt.

Regeln, Kategorien und Budgets sammeln über Jahre Ballast an — eine Regel, die
nie greift, weil eine frühere sie überdeckt; eine Kategorie ohne Buchung seit
zwei Jahren; ein Soll-Betrag für etwas, das es nicht mehr gibt. Nichts davon
verursacht einen Fehler, alles davon macht die App schwerer lesbar.

Bewusst nur BEFUNDE, keine Automatik: die App schlägt vor, gelöscht wird von
Hand. Ein Aufräum-Werkzeug, das selbst entscheidet, ist gefährlicher als der
Ballast, den es entfernt.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import (
    Category,
    CategoryRule,
    StandardBudget,
    Transaction,
    TransactionSplit,
)

# Ab wann gilt eine Kategorie als eingeschlafen. Ein volles Jahr, damit auch
# reine Jahresposten (Ferien, Steuern, Versicherung) nicht fälschlich auffallen.
_STILL_MONATE = 12


def _greift_regel(keyword: str, texte: list[str]) -> int:
    k = (keyword or "").strip().lower()
    if not k:
        return 0
    return sum(1 for t in texte if k in t)


def hygiene_befunde(db: Session, today: date) -> dict:
    """Vier Befundlisten für die Aufräum-Ansicht.

    Alles wird in Python ausgewertet statt per SQL: die Datenmengen sind bei
    einem Einzelnutzer klein, und die Regel-Überdeckung („welche Regel gewinnt")
    hängt an derselben Reihenfolge-Logik wie die Kategorisierung — die will man
    nicht in zwei Sprachen doppelt haben.
    """
    texte = [
        (t or "").lower()
        for t in db.scalars(select(Transaction.description).where(Transaction.description.is_not(None)))
    ]
    regeln = list(db.scalars(select(CategoryRule).order_by(CategoryRule.sort_order, CategoryRule.id)))
    cats = {c.id: c for c in db.scalars(select(Category))}

    # (a) Regeln ohne einen einzigen Treffer im gesamten Bestand.
    ohne_treffer = [
        {"regel": r, "kategorie": cats.get(r.category_id)}
        for r in regeln
        if _greift_regel(r.keyword, texte) == 0
    ]

    # (b) Überdeckte Regeln: eine frühere Regel enthält dasselbe Stichwort als
    #     Teilstring — die spätere kommt damit nie zum Zug.
    ueberdeckt = []
    for i, r in enumerate(regeln):
        k = (r.keyword or "").strip().lower()
        if not k:
            continue
        for frueher in regeln[:i]:
            fk = (frueher.keyword or "").strip().lower()
            if fk and fk in k:
                ueberdeckt.append({
                    "regel": r,
                    "verdeckt_von": frueher,
                    "kategorie": cats.get(r.category_id),
                    "andere_kategorie": cats.get(frueher.category_id),
                })
                break

    # (c) Kategorien ohne Buchung im letzten Jahr (nur Unterkategorien —
    #     Oberkategorien tragen selten direkt Buchungen).
    grenze = add_months(today.replace(day=1), -_STILL_MONATE)
    letzte = dict(
        db.execute(
            select(Transaction.category_id, func.max(Transaction.date))
            .where(Transaction.category_id.is_not(None))
            .group_by(Transaction.category_id)
        ).all()
    )
    # Aufgeteilte Buchungen tragen am Kopf KEINE Kategorie (``category_id`` ist
    # NULL, die Kategorien stecken in den Splits). Ohne diese zweite Abfrage
    # sähe jede Kategorie, die nur über Beleg-Aufteilungen bebucht wird, wie
    # eingeschlafen aus — und die Aufräum-Ansicht schlüge ausgerechnet die zum
    # Löschen vor, die der Quittungs-Scan am fleissigsten füllt.
    for cid, letztes in db.execute(
        select(TransactionSplit.category_id, func.max(Transaction.date))
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(TransactionSplit.category_id.is_not(None))
        .group_by(TransactionSplit.category_id)
    ).all():
        if letztes is not None and (letzte.get(cid) is None or letztes > letzte[cid]):
            letzte[cid] = letztes
    eingeschlafen = []
    for c in cats.values():
        if c.parent_id is None or c.is_archived:
            continue
        zuletzt = letzte.get(c.id)
        if zuletzt is None or zuletzt < grenze:
            eingeschlafen.append({"kategorie": c, "zuletzt": zuletzt})
    eingeschlafen.sort(key=lambda e: (e["zuletzt"] is not None, e["zuletzt"] or date.min))

    # (d) Soll gesetzt, aber seit einem Jahr keine Ausgabe — totes Budget, das
    #     die Monatssumme unnötig aufbläht.
    totes_budget = []
    for sb in db.scalars(select(StandardBudget)):
        if (sb.amount or Decimal("0")) <= 0:
            continue
        c = cats.get(sb.category_id)
        if c is None or c.is_archived:
            continue
        zuletzt = letzte.get(sb.category_id)
        if zuletzt is None or zuletzt < grenze:
            totes_budget.append({"kategorie": c, "betrag": sb.amount, "zuletzt": zuletzt})

    return {
        "ohne_treffer": ohne_treffer,
        "ueberdeckt": ueberdeckt,
        "eingeschlafen": eingeschlafen,
        "totes_budget": totes_budget,
        "anzahl": (
            len(ohne_treffer) + len(ueberdeckt) + len(eingeschlafen) + len(totes_budget)
        ),
        "monate": _STILL_MONATE,
    }
