"""Saldo-Berechnung.

Das Modell trennt zwei Salden eines Kontos:

* ``opening_balance`` — Anfangsbestand zu einem Stichtag (manuell im Konten-Editor)
* ``current_balance`` — abgeleiteter aktueller Stand = opening + Summe der Buchungen

``current_balance`` wird NIE direkt gesetzt, sondern immer aus der Wahrheit
(opening + Buchungen) neu berechnet. Das ist robuster als inkrementelles
Auf-/Abrechnen, weil es bei Bearbeiten/Löschen keine Drift gibt.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.db.models import Account, Transaction


def recalc_account_balance(db: Session, account_id: int) -> None:
    """Setzt ``current_balance`` = ``opening_balance`` + Summe aller Buchungen
    des Kontos.

    ``current_balance`` wird bewusst aus der Wahrheit neu gerechnet (nicht
    inkrementell), damit es bei Bearbeiten/Löschen keine Drift gibt.
    """
    account = db.get(Account, account_id)
    if account is None:
        return

    total = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account_id,
        )
    )
    account.current_balance = (account.opening_balance or Decimal("0")) + Decimal(str(total or 0))
    db.add(account)
