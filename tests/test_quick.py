"""Tests für die Mobile Quick-Add-Seite."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal


def test_quick_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/quick")
    assert resp.status_code == 200
    assert "Schnell erfassen" in resp.text


def test_quick_create_books_transaction(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        acc = Account(name="ZZZ-Quick-Konto", type=AccountType.CASH, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=995)
        db.add(acc)
        db.commit()
        acc_id = acc.id

    resp = logged_in_client.post("/quick", data={
        "kind": "ausgabe", "amount": "12.50", "account_id": str(acc_id), "description": "ZZZ-Kaffee",
    })
    assert resp.status_code == 200
    assert "gespeichert" in resp.text
    with SessionLocal() as db:
        n = db.scalar(select(func.count(Transaction.id)).where(Transaction.account_id == acc_id))
        assert n == 1
        tx = db.scalar(select(Transaction).where(Transaction.account_id == acc_id))
        assert tx.amount == Decimal("-12.50")
