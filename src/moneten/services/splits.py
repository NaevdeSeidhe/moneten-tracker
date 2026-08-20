"""Auto-Split: zentrale Hilfe, damit Kategorie-Auswertungen Aufteilungen sehen.

Eine aufgeteilte Buchung (``Transaction.is_split``) verteilt ihren Betrag auf
mehrere :class:`TransactionSplit`-Anteile mit je eigener Kategorie. Damit Budget,
Vergleich und Geldfluss korrekt rechnen, dürfen sie für solche Buchungen NICHT
die ``category_id`` der Buchung verwenden, sondern die der Splits.

:func:`effective_category_amounts` liefert genau diese „aufgelösten" Zeilen:
für normale Buchungen eine Zeile (Kategorie, Betrag, Datum), für aufgeteilte je
Split eine Zeile. Reine Vorzeichen-Summen (Saldo, Monatszahlen) brauchen das
NICHT — sie nutzen weiterhin den unveränderten Eltern-Betrag.

Wichtig zum Vorzeichen-Filter: Splits tragen dasselbe Vorzeichen wie ihre
Buchung (Ausgabe → negativ). Der ``sign``-Filter greift deshalb auf den
Eltern-Betrag — eine Ausgaben-Buchung wird komplett (mit allen Splits)
einbezogen, eine Einnahme komplett ausgelassen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Transaction, TransactionSplit, not_transfer


def effective_category_amounts(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    sign: str | None = None,
    exclude_transfer: bool = True,
) -> list[tuple[int | None, Decimal, date]]:
    """Kategorie-aufgelöste Beträge im Zeitraum ``[date_from, date_to)``.

    Liefert eine Liste ``(category_id, amount, tx_date)``. Beträge sind
    vorzeichenbehaftet (Ausgabe negativ). Für aufgeteilte Buchungen erscheint
    je Split eine Zeile (mit dessen Kategorie + Anteil), für normale Buchungen
    eine Zeile mit deren Kategorie + Gesamtbetrag.

    ``sign``: ``"expense"`` (nur Ausgaben), ``"income"`` (nur Einnahmen) oder
    ``None`` (alle). ``exclude_transfer``: Umbuchungen auslassen (Default).
    """
    stmt = select(
        Transaction.id,
        Transaction.category_id,
        Transaction.amount,
        Transaction.date,
        Transaction.is_split,
    ).where(Transaction.date >= date_from, Transaction.date < date_to)
    if exclude_transfer:
        stmt = stmt.where(not_transfer())
    if sign == "expense":
        stmt = stmt.where(Transaction.amount < 0)
    elif sign == "income":
        stmt = stmt.where(Transaction.amount > 0)

    base = db.execute(stmt).all()
    split_ids = [r.id for r in base if r.is_split]
    splits_by_tx: dict[int, list[tuple[int | None, Decimal]]] = {}
    if split_ids:
        for sp in db.execute(
            select(
                TransactionSplit.transaction_id,
                TransactionSplit.category_id,
                TransactionSplit.amount,
            ).where(TransactionSplit.transaction_id.in_(split_ids))
        ).all():
            splits_by_tx.setdefault(sp.transaction_id, []).append(
                (sp.category_id, Decimal(str(sp.amount)))
            )

    rows: list[tuple[int | None, Decimal, date]] = []
    for r in base:
        sps = splits_by_tx.get(r.id) if r.is_split else None
        if sps:
            for cid, amt in sps:
                rows.append((cid, amt, r.date))
        else:
            rows.append((r.category_id, Decimal(str(r.amount)), r.date))
    return rows
