"""Budget-Auswertung: Ist-Summen, Median-Vorschläge und Ampel-Logik.

* **Ist** = tatsächliche Ausgaben einer Kategorie in einem Monat (Absolutbetrag).
* **Median-Vorschlag** = Median der Ist-Werte der letzten N Monate — robuster
  als der Mittelwert gegen Ausreisser (Konzept Abschnitt 8).
* **Ampel** = Soll/Ist-Verhältnis: grün < 80 %, gelb 80–100 %, rot > 100 %.

Transfers (management_type=TRANSFER) und Einnahmen werden ausgeschlossen —
das Budget betrachtet nur echte Ausgaben.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import (
    BudgetInterval,
    Category,
    ManagementType,
    StandardBudget,
)
from moneten.services.splits import effective_category_amounts

# Top-Kategorien, die kein Ausgaben-Budget bekommen (gespiegelt im Budget-Router).
_NON_BUDGET_MGMT = {ManagementType.EINKOMMEN, ManagementType.TRANSFER}


def monthly_equivalent(amount: Decimal | None, interval: BudgetInterval) -> Decimal:
    """Monatlicher Soll-Anteil eines Standard-Budgets.

    Bei jährlichem Intervall fliesst 1/12 des Jahresbetrags ins Monatsbudget;
    bei monatlichem Intervall der Betrag selbst.
    """
    if not amount:
        return Decimal("0")
    if interval == BudgetInterval.JAEHRLICH:
        return (amount / 12).quantize(Decimal("0.01"))
    return amount


def month_bounds(month_start: date) -> tuple[date, date]:
    """(erster Tag dieses Monats, erster Tag nächster Monat)."""
    return month_start.replace(day=1), add_months(month_start.replace(day=1), 1)


def ist_for_category(db: Session, category_id: int, month_start: date) -> Decimal:
    """Tatsächliche Netto-Ausgaben einer Kategorie im Monat.

    Keine Transfers. Aufgeteilte Buchungen zählen mit ihrem Split-Anteil.
    **Gutschriften (positive Beträge) werden gegengerechnet** — eine Rückerstattung
    reduziert die Ausgabe. Ergebnis nie negativ (mind. 0).
    """
    start, end = month_bounds(month_start)
    rows = effective_category_amounts(db, date_from=start, date_to=end)
    net = sum((amt for cid, amt, _ in rows if cid == category_id), Decimal("0"))
    return (-net) if net < 0 else Decimal("0")


def ist_map(db: Session, oldest_month_start: date, until_exclusive: date) -> dict[tuple[int, date], Decimal]:
    """Ist-(Netto-)Ausgaben je (Kategorie, Monat) für ein ganzes Fenster — **eine** Query.

    Ersetzt N×M Einzelabfragen (pro Kategorie/Monat) auf der Budget-Seite.
    Aufgeteilte Buchungen werden je Split der passenden Kategorie zugerechnet;
    **Gutschriften werden gegengerechnet** (reduzieren die Monats-Ausgabe der
    Kategorie). Werte sind positiv (≥0); reine Einnahmen-Kategorien ergeben 0.
    """
    rows = effective_category_amounts(db, date_from=oldest_month_start, date_to=until_exclusive)
    out: dict[tuple[int, date], Decimal] = {}
    for category_id, amount, tx_date in rows:
        if category_id is None:
            continue
        key = (category_id, tx_date.replace(day=1))
        out[key] = out.get(key, Decimal("0")) + amount  # vorzeichenbehaftet netto
    # Ist = negativer Netto-Betrag (Ausgabe); Gutschriften gegengerechnet, nie < 0.
    return {k: ((-v) if v < 0 else Decimal("0")) for k, v in out.items()}


def median_from_map(
    imap: dict[tuple[int, date], Decimal], category_id: int, month_start: date, lookback: int = 6
) -> Decimal | None:
    """Median der Ist-Werte der letzten ``lookback`` Monate aus einer :func:`ist_map`."""
    werte: list[Decimal] = []
    for i in range(1, lookback + 1):
        v = imap.get((category_id, add_months(month_start.replace(day=1), -i)))
        if v and v > 0:
            werte.append(v)
    if not werte:
        return None
    # **Ohne Umweg über float.** Vorher lief der Median durch ``float(v)`` →
    # ``statistics.median`` → ``Decimal(str(...))``. Bei den zwei Werten
    # 11'358.17 und 8'444.82 ergab das ``9901.494999999999`` und damit 9901.49
    # statt 9901.50 — ein Rappen, an 200'000 Zufallsfällen nachgemessen in
    # 5.6 % der Fälle; beim Füllen ganzer Franken kippte in 0.025 % der Fälle
    # der Franken.
    #
    # Der Median ist bei ungerader Anzahl der mittlere Wert, bei gerader das
    # Mittel der beiden mittleren. Beides bleibt in ``Decimal`` exakt, und
    # gerundet wird EINMAL am Ende — kaufmännisch, wie es ``lohn.py`` als
    # Konvention des Projekts beschreibt.
    geordnet = sorted(werte)
    mitte = len(geordnet) // 2
    roh = (geordnet[mitte] if len(geordnet) % 2
           else (geordnet[mitte - 1] + geordnet[mitte]) / 2)
    return roh.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def autofill_standard_budgets(db: Session, month_start: date, *, lookback: int = 6) -> int:
    """Setzt für Kategorien **ohne** Standard-Soll einen Vorschlag aus dem Median
    der letzten ``lookback`` Monate (auf ganze Franken gerundet, monatliches
    Intervall).

    Überschreibt vorhandene Standard-Soll **nie** — füllt nur Lücken. Betrachtet
    nur budget-relevante Unterkategorien (Eltern nicht Einkommen/Transfer). Läuft
    serverseitig (Button auf der Budget-Seite). Gibt die Anzahl neu gesetzter
    Kategorien zurück.
    """
    month_start = month_start.replace(day=1)
    have = {sb.category_id for sb in db.scalars(select(StandardBudget))}
    imap = ist_map(db, add_months(month_start, -lookback), add_months(month_start, 1))
    cats = {c.id: c for c in db.scalars(select(Category))}

    created = 0
    for c in cats.values():
        if c.parent_id is None or c.is_archived or c.id in have:
            continue
        top = cats.get(c.parent_id)
        if top is None or top.management_type in _NON_BUDGET_MGMT:
            continue
        median = median_from_map(imap, c.id, month_start, lookback)
        if not median or median <= 0:
            continue
        betrag = median.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if betrag <= 0:
            betrag = Decimal("1")
        db.add(StandardBudget(category_id=c.id, amount=betrag, interval=BudgetInterval.MONATLICH))
        have.add(c.id)
        created += 1
    db.commit()
    return created


def ampel_status(soll: Decimal | None, ist: Decimal) -> str:
    """Liefert 'ok' | 'warn' | 'over' | 'none' für die Soll/Ist-Ampel.

    * none — kein Soll gesetzt
    * ok   — Ist < 80 % vom Soll
    * warn — 80–100 %
    * over — > 100 %
    """
    if soll is None or soll <= 0:
        return "none"
    ratio = float(ist) / float(soll)
    if ratio < 0.8:
        return "ok"
    if ratio <= 1.0:
        return "warn"
    return "over"
