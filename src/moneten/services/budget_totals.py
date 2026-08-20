"""Soll, Ist und Rest eines Monats — die eine gültige Definition.

Diese Zahl steht an zwei Orten: als Leitzahl „Noch frei" auf der Budget-Seite und
als „Budget-Rest (Monat)" auf der Schnell-Erfassen-Seite, die am Handy oft die
erste ist, die man sieht. Beide rechneten sie vorher getrennt — und verschieden:

* Das **Soll** war auf ``/quick`` die Summe *aller* Standard-Budgets, ohne
  Rücksicht auf archivierte Kategorien und ohne Monats-Override.
* Das **Ist** war dort die rohe Summe aller negativen Buchungen. Auf der
  Budget-Seite dagegen kategorie-aufgelöst: Splits aufgeschlüsselt, Gutschriften
  gegengerechnet, je Kategorie bei null gekappt.

Der sichtbare Unterschied waren die unkategorisierten Buchungen — sie zählten auf
dem Handy voll gegen das Budget, auf der Budget-Seite gar nicht. Dieselbe Frage
darf nicht zwei Antworten haben, deshalb liegt die Rechnung jetzt hier.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import Budget, BudgetInterval, Category, ManagementType, StandardBudget
from moneten.services.median_budget import ist_map, monthly_equivalent

# Top-Kategorien, die kein Ausgaben-Budget bekommen (Einnahmen sind kein Soll).
NICHT_BUDGET = {ManagementType.EINKOMMEN, ManagementType.TRANSFER}


def budget_kategorien(db: Session) -> set[int]:
    """IDs der Unterkategorien, die überhaupt ins Monatsbudget zählen.

    Nicht archiviert und nicht unter einer Einnahmen-/Transfer-Oberkategorie.
    """
    tops = {
        c.id: c
        for c in db.scalars(select(Category).where(Category.parent_id.is_(None)))
    }
    ids: set[int] = set()
    for c in db.scalars(select(Category).where(Category.parent_id.is_not(None))):
        if c.is_archived:
            continue
        top = tops.get(c.parent_id)
        if top is not None and top.management_type not in NICHT_BUDGET:
            ids.add(c.id)
    return ids


def soll_map(db: Session, month_start: date) -> dict[int, Decimal]:
    """Effektives Monats-Soll je Kategorie-ID.

    Ein gesetzter Monats-Override schlägt das Standard-Soll; Jahresbeträge
    zählen mit einem Zwölftel.
    """
    erlaubt = budget_kategorien(db)
    std = {sb.category_id: sb for sb in db.scalars(select(StandardBudget))}
    ovr = {
        b.category_id: b.planned_amount
        for b in db.scalars(select(Budget).where(Budget.month == month_start))
    }

    out: dict[int, Decimal] = {}
    for cid in erlaubt:
        override = ovr.get(cid)
        if override is not None and override > 0:
            out[cid] = override
            continue
        sb = std.get(cid)
        if sb is not None and sb.amount > 0:
            out[cid] = monthly_equivalent(sb.amount, sb.interval or BudgetInterval.MONATLICH)
    return out


def anteil_prozent(teil: Decimal, ganzes: Decimal) -> int:
    """Anteil in ganzen Prozent, gekappt auf 0–100.

    Ohne gesetztes Soll gibt es keinen Füllstand, den ein Balken zeigen könnte —
    er stünde auf 0 % und behauptete damit „0 von 0". Der Anteil an den
    Monatsausgaben ist die Zahl, die es stattdessen gibt.

    Bewusst der Anteil und nicht der Vergleich mit dem Vormonat: Zähler und
    Nenner umfassen hier denselben Zeitraum. Ein Vormonatsvergleich stellt am
    5. eines Monats fünf Tage einem ganzen Monat gegenüber und meldet einen
    Rückgang, den es nicht gibt.
    """
    if ganzes <= 0 or teil <= 0:
        return 0
    prozent = int((teil / ganzes * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return min(prozent, 100)


def monats_totale(db: Session, month_start: date) -> dict:
    """``{"soll", "ist", "rest"}`` für einen Monat — Rest kann negativ sein."""
    erlaubt = budget_kategorien(db)
    soll_je_kat = soll_map(db, month_start)
    ist_je_kat = ist_map(db, month_start, add_months(month_start, 1))

    soll = sum(soll_je_kat.values(), Decimal("0"))
    # ``ist_map`` liefert bereits kategorie-aufgelöst und bei null gekappt; hier
    # zählt nur, was auch auf der Budget-Seite in einer Gruppe erscheint.
    ist = sum(
        (betrag for (cid, _m), betrag in ist_je_kat.items() if cid in erlaubt),
        Decimal("0"),
    )
    return {"soll": soll, "ist": ist, "rest": soll - ist}
