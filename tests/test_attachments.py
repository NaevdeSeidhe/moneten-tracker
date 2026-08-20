"""Tests für die Quittungs-Anbindung per Ordner-Referenz (kein Upload)."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services.attachments import parse_date_from_name


def _make_tx() -> int:
    with SessionLocal() as db:
        acc = Account(name="Att-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=700)
        db.add(acc)
        db.flush()
        tx = Transaction(account_id=acc.id, date=date.today(), amount=Decimal("-20.00"),
                         description="Beleg-Test")
        db.add(tx)
        db.commit()
        return tx.id


def test_parse_date_from_name() -> None:
    assert parse_date_from_name("20260515_Migros.pdf") == date(2026, 5, 15)
    assert parse_date_from_name("Rechnung 2026-05-15.pdf") == date(2026, 5, 15)
    assert parse_date_from_name("Beleg_15.05.2026.jpg") == date(2026, 5, 15)
    assert parse_date_from_name("IMG_1234.jpg") is None


def test_assign_receipt_by_name(logged_in_client: TestClient) -> None:
    """Ohne konfigurierten Ordner: nur Dateiname wird vermerkt (file_path bleibt leer)."""
    from moneten.config import settings
    old = settings.receipts_dir
    settings.receipts_dir = None  # explizit kein Ordner (unabhängig von .env)
    try:
        tx_id = _make_tx()
        resp = logged_in_client.post(
            f"/transactions/{tx_id}/attachment",
            data={"filename": "20260515_Migros.pdf"},
        )
        assert resp.status_code == 200
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))
            assert att is not None
            assert att.original_name == "20260515_Migros.pdf"
            # Kein Ordner konfiguriert -> kein file_path, keine Datei kopiert.
            assert att.file_path is None
    finally:
        settings.receipts_dir = old


def test_assign_from_configured_folder(logged_in_client: TestClient, tmp_path: Path) -> None:
    """Mit konfiguriertem Ordner: Datei wird gefunden, Pfad vermerkt, ausgeliefert."""
    # Temporären Quittungs-Ordner mit einer Datei einrichten.
    receipt = tmp_path / "20260520_Coop.pdf"
    receipt.write_bytes(b"%PDF-1.4 test")

    from moneten.config import settings
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        tx_id = _make_tx()
        resp = logged_in_client.post(
            f"/transactions/{tx_id}/attachment",
            data={"filename": "20260520_Coop.pdf"},
        )
        assert resp.status_code == 200
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))
            assert att.file_path is not None
            att_id = att.id
        # Datei kann ausgeliefert werden (liegt im konfigurierten Ordner).
        served = logged_in_client.get(f"/transactions/attachment/{att_id}")
        assert served.status_code == 200
        assert served.content == b"%PDF-1.4 test"
    finally:
        settings.receipts_dir = old


def test_serve_rejects_outside_folder(logged_in_client: TestClient, tmp_path: Path) -> None:
    """Pfad-Traversal-Schutz: Dateien ausserhalb des Ordners werden nicht ausgeliefert."""
    from moneten.config import settings
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path  # leerer Ordner
    try:
        tx_id = _make_tx()
        # Attachment mit Pfad ausserhalb des Ordners direkt anlegen.
        with SessionLocal() as db:
            att = Attachment(transaction_id=tx_id, file_path=os.path.abspath(__file__),
                             original_name="geheim.py")
            db.add(att)
            db.commit()
            att_id = att.id
        served = logged_in_client.get(f"/transactions/attachment/{att_id}")
        assert served.status_code == 404
    finally:
        settings.receipts_dir = old


def test_indicator_and_unassign(logged_in_client: TestClient) -> None:
    tx_id = _make_tx()
    logged_in_client.post(f"/transactions/{tx_id}/attachment", data={"filename": "bon.pdf"})
    page = logged_in_client.get("/transactions")
    # Indikator ist jetzt ein klickbares Quittungs-Icon (öffnet Detail-Popup).
    assert "tx-receipt" in page.text and "bon.pdf" in page.text

    with SessionLocal() as db:
        att_id = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id)).id
    resp = logged_in_client.post(f"/transactions/attachment/{att_id}/delete")
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.get(Attachment, att_id) is None
