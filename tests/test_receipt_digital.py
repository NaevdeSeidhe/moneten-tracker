"""Tests für die digitale Quittung: Analyse, Lernen, nachträgliches Matching.

Bewusst OHNE Tesseract — wir füttern den Belegtext direkt (OcrResult), sodass die
Logik (Positions-/Kategorie-Erkennung, Lern-Regeln, Auto-Match) deterministisch
geprüft wird. Das echte Foto-OCR wird auf dem NAS verifiziert.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import (
    Account,
    Attachment,
    Category,
    PendingReceipt,
    ReceiptItemRule,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services.receipt_digital import analyze, learn, match_pending, save_receipt
from moneten.services.receipt_ocr import OcrResult

_SAMPLE = (
    "MIGROS MM MUSTERSTADT\n"
    "06.06.2026 14:32\n"
    "Tomaten Rispen 500g      3.95\n"
    "Vollmilch 1L             1.60\n"
    "Lindt Excellence 70%     3.95\n"
    "TOTAL                    9.50\n"
)


def _first_category_id(db) -> int:
    return db.scalar(select(Category.id).where(Category.parent_id.is_not(None)))


def test_analyze_extracts_merchant_items_and_learned_category() -> None:
    with SessionLocal() as db:
        cid = _first_category_id(db)
        # Lern-Regel: „tomate" → cid. analyze() muss sie auf „Tomaten …" anwenden.
        db.add(ReceiptItemRule(keyword="tomate", merchant_key="", category_id=cid))
        db.commit()

        ocr = OcrResult(text=_SAMPLE, method="text-layer", amount=Decimal("9.50"), date=date(2026, 6, 6))
        result = analyze(db, ocr)

        assert "MIGROS" in result["merchant"].upper()
        names = [it["name"] for it in result["items"]]
        assert any("Tomaten" in n for n in names)
        assert any("Vollmilch" in n for n in names)
        tomato = next(it for it in result["items"] if "Tomaten" in it["name"])
        assert tomato["category_id"] == cid  # gelernte Regel hat gegriffen
        assert result["amount"] == "9.50"
        assert result["date"] == "2026-06-06"


def test_learn_upserts_rules() -> None:
    with SessionLocal() as db:
        cid = _first_category_id(db)
        other = db.scalar(
            select(Category.id).where(Category.parent_id.is_not(None), Category.id != cid)
        )
        structured = {"merchant_key": "coop", "items": [{"name": "Bananen Bio", "price": "2.35", "category_id": cid}]}
        learn(db, structured)
        db.commit()
        rule = db.scalar(select(ReceiptItemRule).where(ReceiptItemRule.keyword == "bananen", ReceiptItemRule.merchant_key == "coop"))
        assert rule is not None and rule.category_id == cid
        # Erneut mit anderer Kategorie → Update (kein Duplikat).
        structured["items"][0]["category_id"] = other
        learn(db, structured)
        db.commit()
        rules = db.scalars(select(ReceiptItemRule).where(ReceiptItemRule.keyword == "bananen", ReceiptItemRule.merchant_key == "coop")).all()
        assert len(rules) == 1 and rules[0].category_id == other
        # Generische Regel (mk="") wurde zusätzlich gelernt.
        assert db.scalar(select(ReceiptItemRule).where(
            ReceiptItemRule.keyword == "bananen", ReceiptItemRule.merchant_key == "")) is not None


def test_save_receipt_with_duplicate_item_keyword() -> None:
    """Zwei Positionen mit gleichem Stichwort (z. B. zwei „Nussbrot …" → „nussbrot")
    dürfen den Beleg NICHT sprengen. Sonst zweites INSERT mit gleichem (keyword,
    merchant_key) → UNIQUE-Verletzung beim Commit → Beleg liesse sich nicht speichern."""
    with SessionLocal() as db:
        cid = _first_category_id(db)
        structured = {
            "merchant_key": "coop",
            # Der Total muss die Positionen ergeben, sonst hält die Gegenprobe die
            # Liste zurück und es gäbe nichts zu lernen (siehe test_receipt_gegenprobe).
            "amount": "12.90",
            "items": [
                {"name": "Nussbrot Habelfl./K", "price": "6.95", "category_id": cid},
                {"name": "Nussbrot Le Gruyere", "price": "5.95", "category_id": cid},
            ],
        }
        # Darf NICHT werfen (vorher: IntegrityError UNIQUE keyword, merchant_key).
        save_receipt(db, structured, "Coop\nNussbrot Habelfl./K 6.95\nNussbrot Le Gruyere 5.95", source="photo")
        rules = db.scalars(
            select(ReceiptItemRule).where(ReceiptItemRule.keyword == "nussbrot")
        ).all()
        # je genau eine Regel für ("nussbrot","coop") und die generische ("nussbrot","").
        assert sorted((r.merchant_key or "") for r in rules) == ["", "coop"]


def test_learn_generic_applies_across_merchant_change() -> None:
    """Eine gelernte Kategorie greift auch, wenn der Händler beim NÄCHSTEN Beleg anders
    erkannt wird (Karma mal „Karma", mal „Coop") — dank generischer Regel (mk="")."""
    from moneten.services.categorization import load_active_rules
    from moneten.services.receipt_digital import categorize_item, merchant_key

    with SessionLocal() as db:
        cid = _first_category_id(db)
        learn(db, {"merchant_key": merchant_key("Karma"),
                   "items": [{"name": "Draftgetraenk Spezial", "price": "11.95", "category_id": cid}]})
        db.commit()
        cats = list(db.scalars(select(Category)))
        learned = [(r.keyword, r.merchant_key or "", r.category_id)
                   for r in db.scalars(select(ReceiptItemRule))]
        # Händler diesmal als „Coop" erkannt → die generische Regel muss greifen.
        got = categorize_item("Draftgetraenk Spezial", learned=learned,
                              merchant_k=merchant_key("Coop"), cats=cats, pairs=load_active_rules(db))
        assert got == cid


def test_save_receipt_pending_then_match_after_booking() -> None:
    with SessionLocal() as db:
        acc = db.scalar(select(Account))
        amount = Decimal("87.65")  # distinktiv, kollidiert nicht mit Seeds
        structured = {
            "merchant": "Denner", "merchant_key": "denner",
            "date": "2026-06-05", "amount": str(amount),
            "items": [{"name": "Wein", "price": "87.65", "category_id": _first_category_id(db)}],
        }
        # 1) Noch keine passende Buchung → vorgemerkt.
        res = save_receipt(db, structured, "Denner\nWein 87.65", source="photo")
        assert res["attached_tx_id"] is None and res["pending_id"] is not None
        assert db.get(PendingReceipt, res["pending_id"]) is not None

        # 2) Buchung taucht auf → match_pending hängt den Beleg an und löscht das Pending.
        tx = Transaction(account_id=acc.id, amount=-amount, date=date(2026, 6, 6), description="DENNER")
        db.add(tx)
        db.commit()
        n = match_pending(db)
        assert n == 1
        assert db.get(PendingReceipt, res["pending_id"]) is None
        att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx.id))
        assert att is not None and att.file_path is None and att.parsed_items_json


def test_save_receipt_attaches_immediately_when_booking_exists() -> None:
    with SessionLocal() as db:
        acc = db.scalar(select(Account))
        amount = Decimal("44.40")
        tx = Transaction(account_id=acc.id, amount=-amount, date=date(2026, 6, 10), description="SPAR")
        db.add(tx)
        db.commit()
        structured = {
            "merchant": "Spar", "merchant_key": "spar", "date": "2026-06-10", "amount": str(amount),
            "items": [{"name": "Brot", "price": "44.40", "category_id": _first_category_id(db)}],
        }
        res = save_receipt(db, structured, "Spar", source="photo")
        assert res["attached_tx_id"] == tx.id
        assert db.scalar(select(Attachment).where(Attachment.transaction_id == tx.id)) is not None
