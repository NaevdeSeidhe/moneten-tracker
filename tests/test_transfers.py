"""Tests für Konto-Transfers / Umbuchungen (Phase 1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal


def _make_account(name: str, opening: str = "0", typ: AccountType = AccountType.BANK) -> int:
    with SessionLocal() as db:
        acc = Account(name=name, type=typ, currency="CHF",
                      opening_balance=Decimal(opening), current_balance=Decimal(opening))
        db.add(acc)
        db.commit()
        return acc.id


def test_transfer_moves_balance(logged_in_client: TestClient) -> None:
    src = _make_account("TR-Bank", "1000")
    dst = _make_account("TR-Kasse", "0", AccountType.CASH)

    resp = logged_in_client.post(
        "/transactions/transfer",
        data={"from_account_id": str(src), "to_account_id": str(dst),
              "amount": "200", "date": date.today().isoformat(), "notes": "Bargeld"},
    )
    assert resp.status_code == 200

    with SessionLocal() as db:
        assert db.get(Account, src).current_balance == Decimal("800.00")   # 1000 - 200
        assert db.get(Account, dst).current_balance == Decimal("200.00")   # 0 + 200
        # Genau zwei verknüpfte Buchungen mit gemeinsamer transfer_group_id.
        txs = list(db.scalars(select(Transaction).where(Transaction.account_id.in_([src, dst]))))
        assert len(txs) == 2
        assert txs[0].transfer_group_id == txs[1].transfer_group_id
        assert txs[0].transfer_group_id is not None


def test_transfer_same_account_rejected(logged_in_client: TestClient) -> None:
    acc = _make_account("TR-Solo", "100")
    resp = logged_in_client.post(
        "/transactions/transfer",
        data={"from_account_id": str(acc), "to_account_id": str(acc),
              "amount": "50", "date": date.today().isoformat()},
    )
    assert resp.status_code == 400
    assert "unterschiedlich" in resp.text


def test_transfer_excluded_from_month_totals() -> None:
    from moneten.routers.dashboard import _month_totals

    src = _make_account("TR-A", "500")
    dst = _make_account("TR-B", "0")
    today = date.today()
    with SessionLocal() as db:
        income_before, expense_before, _ = _month_totals(db, today)

    # Transfer anlegen (über die Service-Logik via Client wäre auch möglich;
    # hier direkt, um isoliert zu prüfen).
    import uuid

    from moneten.db.models import ManagementType
    gid = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(Transaction(account_id=src, date=today, amount=Decimal("-300"),
                           description="x", management_type=ManagementType.TRANSFER, transfer_group_id=gid))
        db.add(Transaction(account_id=dst, date=today, amount=Decimal("300"),
                           description="y", management_type=ManagementType.TRANSFER, transfer_group_id=gid))
        db.commit()
        income_after, expense_after, _ = _month_totals(db, today)

    # Transfer darf weder Eingang noch Ausgang verändern.
    assert income_after == income_before
    assert expense_after == expense_before


def test_transfer_delete_removes_both(logged_in_client: TestClient) -> None:
    src = _make_account("TR-Del-A", "1000")
    dst = _make_account("TR-Del-B", "0")
    logged_in_client.post(
        "/transactions/transfer",
        data={"from_account_id": str(src), "to_account_id": str(dst),
              "amount": "150", "date": date.today().isoformat()},
    )
    with SessionLocal() as db:
        one = db.scalar(select(Transaction).where(Transaction.account_id == src,
                                                  Transaction.transfer_group_id.isnot(None)))
        tx_id = one.id

    resp = logged_in_client.post(f"/transactions/{tx_id}/delete")
    assert resp.status_code == 200

    with SessionLocal() as db:
        # Beide Seiten weg, Salden zurückgesetzt.
        remaining = list(db.scalars(select(Transaction).where(Transaction.account_id.in_([src, dst]))))
        assert remaining == []
        assert db.get(Account, src).current_balance == Decimal("1000.00")
        assert db.get(Account, dst).current_balance == Decimal("0.00")
