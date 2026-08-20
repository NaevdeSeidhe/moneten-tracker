"""Tests für Auto-Split: Buchung auf mehrere Kategorie-Anteile aufteilen.

Deckt ab: Datenmodell + Auswertungs-Helfer (Budget-Ist, effektive Beträge),
die Speicher-/Aufheben-Routen mit Summen-Validierung, dass aufgeteilte
Buchungen NICHT als unkategorisiert gelten, sowie die Beleg-Positions-Erkennung.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.dates import add_months
from moneten.db.models import (
    Account,
    AccountType,
    Attachment,
    Category,
    ManagementType,
    Transaction,
    TransactionSplit,
)
from moneten.db.session import SessionLocal
from moneten.services.categorization import uncategorized_groups
from moneten.services.median_budget import ist_for_category
from moneten.services.receipt_split import parse_receipt_items, suggest_splits
from moneten.services.splits import effective_category_amounts


def _account() -> int:
    with SessionLocal() as db:
        acc = Account(name="Split-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=900)
        db.add(acc)
        db.commit()
        return acc.id


def _fresh_cats() -> tuple[int, int]:
    """Zwei eigene Kategorien (eindeutig) — keine Kollision mit anderen Tests."""
    with SessionLocal() as db:
        a = Category(name="SplitTest-A", management_type=ManagementType.BARGELD)
        b = Category(name="SplitTest-B", management_type=ManagementType.BARGELD)
        db.add_all([a, b])
        db.commit()
        return a.id, b.id


def test_effective_amounts_distributes_split() -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=m, amount=Decimal("-78.40"),
                         description="Migros Split", is_split=True)
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, category_id=ca, amount=Decimal("-56.80")))
        db.add(TransactionSplit(transaction_id=tx.id, category_id=cb, amount=Decimal("-21.60")))
        db.commit()

        rows = effective_category_amounts(db, date_from=m, date_to=add_months(m, 1), sign="expense")
        by_cat = {ca: Decimal("0"), cb: Decimal("0")}
        for cid, amt, _ in rows:
            if cid in by_cat:
                by_cat[cid] += amt
        assert by_cat[ca] == Decimal("-56.80")
        assert by_cat[cb] == Decimal("-21.60")
        # Die Split-Anteile summieren sich exakt zum Eltern-Betrag — der Eltern-Betrag
        # selbst (mit Kategorie None) taucht NICHT zusätzlich auf, sonst wäre die Summe
        # über die zwei eigenen Kategorien nicht −78.40.
        assert by_cat[ca] + by_cat[cb] == Decimal("-78.40")


def test_ist_for_category_uses_split_share() -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=m, amount=Decimal("-50.00"),
                         description="Einkauf", is_split=True)
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, category_id=ca, amount=Decimal("-30.00")))
        db.add(TransactionSplit(transaction_id=tx.id, category_id=cb, amount=Decimal("-20.00")))
        db.commit()
        assert ist_for_category(db, ca, m) == Decimal("30.00")
        assert ist_for_category(db, cb, m) == Decimal("20.00")


def test_split_parent_not_uncategorized() -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        split = Transaction(account_id=acc, category_id=None, date=m, amount=Decimal("-12.00"),
                            description="ZZSPLITONLY Laden", is_split=True)
        normal = Transaction(account_id=acc, category_id=None, date=m, amount=Decimal("-9.00"),
                             description="ZZNORMALONLY Laden")
        db.add_all([split, normal])
        db.flush()
        db.add(TransactionSplit(transaction_id=split.id, category_id=ca, amount=Decimal("-7.00")))
        db.add(TransactionSplit(transaction_id=split.id, category_id=cb, amount=Decimal("-5.00")))
        db.commit()

        groups = uncategorized_groups(db, limit=500)
        labels = " ".join(g.label for g in groups).lower()
        assert "zznormalonly" in labels  # normale unkategorisierte Buchung erscheint
        assert "zzsplitonly" not in labels  # aufgeteilte gilt als zugeordnet


def test_save_split_route(logged_in_client: TestClient) -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-30.00"), description="Aufteilen")
        db.add(tx)
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(
        f"/transactions/{tx_id}/split",
        data={"split_cat": [str(ca), str(cb)], "split_amount": ["20.00", "10.00"]},
    )
    assert r.status_code == 200

    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        assert tx.is_split is True
        assert tx.category_id is None
        splits = db.scalars(select(TransactionSplit).where(TransactionSplit.transaction_id == tx_id)).all()
        assert len(splits) == 2
        assert sum((s.amount for s in splits), Decimal("0")) == Decimal("-30.00")
        assert all(s.amount < 0 for s in splits)  # Vorzeichen wie Buchung (Ausgabe)


def test_save_split_rejects_mismatch(logged_in_client: TestClient) -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-30.00"), description="Aufteilen2")
        db.add(tx)
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(
        f"/transactions/{tx_id}/split",
        data={"split_cat": [str(ca), str(cb)], "split_amount": ["20.00", "5.00"]},  # Summe 25 ≠ 30
    )
    assert r.status_code == 400
    assert "muss dem Buchungsbetrag" in r.text
    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        assert tx.is_split is False  # nichts gespeichert


def test_clear_split_route(logged_in_client: TestClient) -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-40.00"), description="Aufheben", is_split=True)
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, category_id=ca, amount=Decimal("-25.00")))
        db.add(TransactionSplit(transaction_id=tx.id, category_id=cb, amount=Decimal("-15.00")))
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(f"/transactions/{tx_id}/split/clear")
    assert r.status_code == 200
    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        assert tx.is_split is False
        rest = db.scalars(select(TransactionSplit).where(TransactionSplit.transaction_id == tx_id)).all()
        assert rest == []


def test_parse_receipt_items() -> None:
    text = "Bio Bananen 2.95\nMilch Drink 1.65\nRotwein Merlot 12.50\nTOTAL 17.10\nKarte 17.10"
    items = parse_receipt_items(text)
    names = [n for n, _ in items]
    assert len(items) == 3  # Total/Karte werden ausgelassen
    assert any("Banane" in n for n in names)
    assert sum((p for _, p in items), Decimal("0")) == Decimal("17.10")


def test_suggest_split_route_renders(logged_in_client: TestClient) -> None:
    acc = _account()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-17.10"), description="Migros Beleg")
        db.add(tx)
        db.flush()
        db.add(Attachment(transaction_id=tx.id, original_name="bon.pdf",
                          ocr_text="Bananen 2.95\nMilch 1.65\nWein 12.50"))
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(f"/transactions/{tx_id}/split/suggest")
    assert r.status_code == 200
    assert "split-editor" in r.text
    assert "Aufteilung in Kategorien" in r.text
    assert "Positionen erkannt" in r.text  # Vorschlags-Hinweis


def test_transactions_page_shows_split_pill(logged_in_client: TestClient) -> None:
    acc = _account()
    ca, cb = _fresh_cats()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-15.00"), description="Aufgeteilte Buchung XY", is_split=True)
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, category_id=ca, amount=Decimal("-10.00")))
        db.add(TransactionSplit(transaction_id=tx.id, category_id=cb, amount=Decimal("-5.00")))
        db.commit()

    r = logged_in_client.get("/transactions")
    assert r.status_code == 200
    assert "Aufgeteilt ·" in r.text  # Listen-Pille


def test_suggest_splits_sums_to_target() -> None:
    acc = _account()
    text = "Bananen 2.95\nMilch 1.65\nWein 12.50"
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, category_id=None, date=date.today(),
                         amount=Decimal("-17.10"), description="Migros")
        result = suggest_splits(db, tx, text)
    assert result["rows"]
    total = sum((r["amount"] for r in result["rows"]), Decimal("0"))
    assert total == Decimal("17.10")  # exakt am Buchungsbetrag
