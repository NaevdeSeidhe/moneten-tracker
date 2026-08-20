"""Test für die Bargeldkasse-Inventur (Kassensturz → Korrektur-Buchung)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal


def test_cash_inventory_books_difference(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        acc = Account(name="ZZZ-Kassette", type=AccountType.CASH, currency="CHF",
                      opening_balance=Decimal("100.00"), current_balance=Decimal("100.00"), sort_order=990)
        db.add(acc)
        db.commit()
        acc_id = acc.id

    # Gezählt: 85.00 → Differenz −15.00 wird gebucht.
    resp = logged_in_client.post(f"/accounts/{acc_id}/inventory", data={"counted": "85.00"})
    assert resp.status_code == 200

    with SessionLocal() as db:
        acc = db.get(Account, acc_id)
        assert acc.current_balance == Decimal("85.00")  # Saldo entspricht jetzt der Zählung
        corr = db.scalar(
            select(Transaction).where(Transaction.account_id == acc_id)
        )
        assert corr is not None
        assert corr.amount == Decimal("-15.00")
        assert "Kassensturz" in corr.description
