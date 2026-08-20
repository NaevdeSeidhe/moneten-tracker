"""Vergleiche: Monat-zu-Monat und Jahr-zu-Jahr.

Liefert Einnahmen/Ausgaben/Saldo zweier Zeiträume plus — für den Monatsvergleich
— die Ausgaben je Top-Kategorie mit Differenz. Transfers (Umbuchungen) zählen
nirgends als Einnahme/Ausgabe.

**Beide Paare laufen auf denselben Stichtag**: der frühere Zeitraum endet am
gleichen Kalendertag wie der laufende. Vorher stand das laufende Jahr bis heute
gegen das VOLLE Vorjahr — im Februar also ein Sechstel gegen ein Ganzes. Jede
Kennzahl behauptete damit einen Einbruch, den es nicht gab. Dieselbe Verzerrung
steckte im Monatspaar (angefangener Monat gegen vollen Vormonat) und ist aus
derselben Ursache mitbehoben.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import Category, Transaction, not_transfer
from moneten.services.splits import effective_category_amounts
from moneten.templating import MONATE


@dataclass
class Totals:
    income: Decimal
    expense: Decimal   # positiv
    saldo: Decimal


def _stichtag_grenze(jahr: int, monat: int, tag: int) -> date:
    """Exklusive obere Grenze für einen Zeitraum, der am ``tag.monat.jahr`` endet.

    Zurück kommt der FOLGETAG, weil :func:`_period_totals` die obere Grenze
    ausschliesst — der Stichtag selbst muss aber mitzählen.

    Existiert der Tag im Zielmonat nicht (29. Februar in einem Nicht-Schaltjahr,
    31. in einem 30-Tage-Monat), endet der Zeitraum mit dem Monat: weiter hinten
    liegt dort ohnehin nichts mehr. Der direkte Weg
    ``date(jahr, monat, tag) + timedelta(days=1)`` wirft an genau diesen Tagen
    ValueError — deshalb zuerst die Monatslänge prüfen statt hinterher fangen.
    """
    letzter_tag = monthrange(jahr, monat)[1]
    if tag >= letzter_tag:
        return add_months(date(jahr, monat, 1), 1)
    return date(jahr, monat, tag + 1)


def _period_totals(db: Session, start: date, end: date) -> Totals:
    """Einnahmen/Ausgaben/Saldo im Zeitraum ``[start, end)`` (ohne Transfers)."""
    income = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.amount > 0, Transaction.date >= start, Transaction.date < end, not_transfer()
        )
    ) or 0
    expense = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.amount < 0, Transaction.date >= start, Transaction.date < end, not_transfer()
        )
    ) or 0
    income = Decimal(str(income))
    expense = Decimal(str(expense)).copy_abs()
    return Totals(income=income, expense=expense, saldo=income - expense)


def _top_name_map(db: Session) -> dict[int, str]:
    """category_id → Name der Top-Kategorie (Wurzel der Hierarchie)."""
    cats = {c.id: c for c in db.scalars(select(Category))}

    def top(cat_id: int) -> str | None:
        c = cats.get(cat_id)
        seen: set[int] = set()
        while c is not None and c.parent_id is not None and c.parent_id in cats and c.id not in seen:
            seen.add(c.id)
            c = cats[c.parent_id]
        return c.name if c is not None else None

    return {cid: top(cid) for cid in cats}


def _expense_by_top(db: Session, start: date, end: date, top_of: dict[int, str]) -> dict[str, Decimal]:
    """Netto-Ausgaben (positiv) je Top-Kategorie im Zeitraum.

    Aufgeteilte Buchungen zählen je Split zur jeweiligen Kategorie. **Gutschriften
    werden gegengerechnet** (netto je Top-Kategorie); nur echte Netto-Ausgaben
    erscheinen (Einnahmen-Tops fallen heraus).
    """
    rows = effective_category_amounts(db, date_from=start, date_to=end)
    net: dict[str, Decimal] = {}
    for cat_id, amount, _ in rows:
        name = top_of.get(cat_id) if cat_id is not None else None
        key = name or "Ohne Kategorie"
        net[key] = net.get(key, Decimal("0")) + amount  # vorzeichenbehaftet
    return {k: (-v) for k, v in net.items() if v < 0}


def build_comparison(db: Session, today: date) -> dict:
    """Baut den kompletten Vergleich (Monat aktuell vs. Vormonat, Jahr vs. Vorjahr).

    ``today`` ist der Stichtag für BEIDE Seiten jedes Paars (siehe Modul-Docstring).
    Der laufende Zeitraum endet damit ebenfalls heute — künftig datierte Buchungen
    im gleichen Monat/Jahr bleiben aussen vor, sonst stünde erneut Ungleiches
    nebeneinander.
    """
    stichtag_ende = _stichtag_grenze(today.year, today.month, today.day)

    m_cur = today.replace(day=1)
    m_prev = add_months(m_cur, -1)
    m_prev_ende = _stichtag_grenze(m_prev.year, m_prev.month, today.day)
    month_cur = _period_totals(db, m_cur, stichtag_ende)
    month_prev = _period_totals(db, m_prev, m_prev_ende)

    top_of = _top_name_map(db)
    # Icon je Top-Kategorie (für die Vergleichs-Visualisierung).
    top_icons = {
        c.name: c.icon
        for c in db.scalars(select(Category).where(Category.parent_id.is_(None)))
    }
    exp_cur = _expense_by_top(db, m_cur, stichtag_ende, top_of)
    exp_prev = _expense_by_top(db, m_prev, m_prev_ende, top_of)
    cats = []
    for name in sorted(set(exp_cur) | set(exp_prev), key=lambda n: exp_cur.get(n, Decimal("0")), reverse=True):
        cur = exp_cur.get(name, Decimal("0"))
        prev = exp_prev.get(name, Decimal("0"))
        cats.append({
            "name": name, "cur": cur, "prev": prev, "delta": cur - prev,
            "icon": top_icons.get(name) or "tag",
        })
    # Balken-Skalierung: alle Werte relativ zum grössten Kategorie-Betrag.
    cmax = max((float(max(c["cur"], c["prev"])) for c in cats), default=0.0)
    for c in cats:
        c["cur_pct"] = round(float(c["cur"]) / cmax * 100, 1) if cmax > 0 else 0
        c["prev_pct"] = round(float(c["prev"]) / cmax * 100, 1) if cmax > 0 else 0

    year_cur = _period_totals(db, date(today.year, 1, 1), stichtag_ende)
    year_prev = _period_totals(
        db,
        date(today.year - 1, 1, 1),
        _stichtag_grenze(today.year - 1, today.month, today.day),
    )

    return {
        "month_cur": month_cur,
        "month_prev": month_prev,
        "month_cats": cats,
        "year_cur": year_cur,
        "year_prev": year_prev,
        "year_cur_label": str(today.year),
        "year_prev_label": str(today.year - 1),
        # Ohne diese Angabe wirkt der gekürzte Vorjahreswert schlicht falsch —
        # die Seite muss den gemeinsamen Stichtag nennen.
        "stichtag_label": f"{today.day}. {MONATE[today.month - 1]}",
        "stichtag_tag": f"{today.day}.",
    }
