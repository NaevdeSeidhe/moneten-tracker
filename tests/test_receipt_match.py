"""Tests für die Quittungs-Auto-Zuordnung (Datum + Betrag → Buchungs-Vorschlag)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import fitz  # PyMuPDF
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.config import settings
from moneten.db.models import Account, AccountType, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services.receipt_match import (
    archive_receipt,
    auto_archive_old,
    auto_match,
    build_suggestions,
    earliest_transaction_date,
    unassigned_receipts,
)
from moneten.services.receipt_ocr import extract_date


def _text_pdf(path, text: str) -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _account() -> int:
    with SessionLocal() as db:
        acc = Account(name="Match-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=900)
        db.add(acc)
        db.commit()
        return acc.id


def test_suggestion_matches_by_date_and_amount(tmp_path) -> None:
    # Quittung: 15.05.2026, Total 78.40
    _text_pdf(tmp_path / "rmatch_2026-05-15_Migros.pdf", "Migros\n15.05.2026\nTotal CHF 78.40")

    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            # passende Buchung (gleiches Datum + Betrag) + Ablenker
            match = Transaction(account_id=acc, date=date(2026, 5, 15), amount=Decimal("-78.40"),
                                description="Migros Einkauf")
            other = Transaction(account_id=acc, date=date(2026, 5, 1), amount=Decimal("-12.00"),
                                description="Anderes")
            db.add_all([match, other])
            db.commit()
            match_id = match.id

            suggestions = build_suggestions(db)
            assert len(suggestions) == 1
            s = suggestions[0]
            assert s.amount == Decimal("78.40")
            assert s.date_guess == date(2026, 5, 15)
            assert s.best is not None
            assert s.best.id == match_id  # exakter Treffer gewinnt
    finally:
        settings.receipts_dir = old


def test_assigned_receipts_disappear(tmp_path) -> None:
    _text_pdf(tmp_path / "rmatch_2026-05-20_Coop.pdf", "Coop\nTotal CHF 43.25")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            tx = Transaction(account_id=acc, date=date(2026, 5, 20), amount=Decimal("-43.25"), description="Coop")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        # Vor Zuordnung: 1 unzugeordnet
        with SessionLocal() as db:
            assert len(unassigned_receipts(db)) == 1

        # Zuordnen via Service
        from moneten.services.receipt_match import attach_receipt
        with SessionLocal() as db:
            attach_receipt(db, db.get(Transaction, tx_id), "rmatch_2026-05-20_Coop.pdf")
            db.commit()

        # Danach: 0 unzugeordnet
        with SessionLocal() as db:
            assert len(unassigned_receipts(db)) == 0
    finally:
        settings.receipts_dir = old


# ----------  Autonomer Auto-Abgleich (nach Import / per Button)  ----------
# Hinweis: Das DB-Schema ist über den Testlauf hinweg gemeinsam (Buchungen
# akkumulieren). Darum hier bewusst eindeutige Beträge/Daten verwenden, damit
# kein anderer Test eine zweite Übereinstimmung erzeugt (Mehrdeutigkeit).
def test_auto_match_attaches_unique_high_confidence(tmp_path) -> None:
    """Eindeutiger sicherer Treffer (exakter Betrag + Datum) wird automatisch
    zugeordnet — ohne Bestätigung, serverseitig."""
    _text_pdf(tmp_path / "rmatch_2026-03-12_Denner.pdf", "Denner\n12.03.2026\nTotal CHF 91.55")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            match = Transaction(account_id=acc, date=date(2026, 3, 12), amount=Decimal("-91.55"),
                                description="Denner Einkauf")
            other = Transaction(account_id=acc, date=date(2026, 3, 1), amount=Decimal("-7.20"),
                                description="Anderes")
            db.add_all([match, other])
            db.commit()
            match_id = match.id

        with SessionLocal() as db:
            assert auto_match(db) == 1

        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == match_id))
            assert att is not None and att.original_name == "rmatch_2026-03-12_Denner.pdf"
            assert len(unassigned_receipts(db)) == 0
    finally:
        settings.receipts_dir = old


def test_auto_match_skips_ambiguous(tmp_path) -> None:
    """Zwei Buchungen mit gleichem Betrag + Datum → kein eindeutiger Treffer,
    daher KEINE automatische Zuordnung (bleibt für den Assistenten)."""
    _text_pdf(tmp_path / "rmatch_2026-02-08_Coop.pdf", "Coop\n08.02.2026\nTotal CHF 137.77")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            a = Transaction(account_id=acc, date=date(2026, 2, 8), amount=Decimal("-137.77"), description="Coop A")
            b = Transaction(account_id=acc, date=date(2026, 2, 8), amount=Decimal("-137.77"), description="Coop B")
            db.add_all([a, b])
            db.commit()

        with SessionLocal() as db:
            assert auto_match(db) == 0
            assert len(unassigned_receipts(db)) == 1  # Quittung bleibt offen
    finally:
        settings.receipts_dir = old


def test_extract_date_from_text() -> None:
    assert extract_date("Migros Musterstadt\nDatum 15.05.2026\nTotal CHF 12.00") == date(2026, 5, 15)
    assert extract_date("Kaufdatum: 2026-05-15") == date(2026, 5, 15)
    assert extract_date("Beleg vom 15. Mai 2026") == date(2026, 5, 15)
    assert extract_date("am 15.05.26 um 12:30") == date(2026, 5, 15)
    assert extract_date("kein datum hier") is None


def test_auto_match_uses_ocr_date_fallback(tmp_path) -> None:
    """Dateiname OHNE Datum, aber Datum + Betrag stehen im Belegtext → wird
    trotzdem automatisch zugeordnet (OCR-Datum als Fallback)."""
    _text_pdf(tmp_path / "Beleg_ohne_Dateinamensdatum.pdf",
              "Migros\nDatum 03.07.2026\nTotal CHF 88.15")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 7, 3), amount=Decimal("-88.15"),
                               description="Migros"))
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 1
            assert len(unassigned_receipts(db)) == 0
    finally:
        settings.receipts_dir = old


def test_auto_match_skips_without_date(tmp_path) -> None:
    """Ohne erkennbares Datum im Dateinamen fehlt die Datums-Bestätigung →
    kein sicherer Treffer, keine automatische Zuordnung."""
    _text_pdf(tmp_path / "Quittung_ohne_Datum.pdf", "Shop\nTotal CHF 63.33")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 4, 9), amount=Decimal("-63.33"),
                               description="Shop"))
            db.commit()

        with SessionLocal() as db:
            assert auto_match(db) == 0
    finally:
        settings.receipts_dir = old


# ----------  Tier 2: eindeutiger Betrag im weiten Datumsfenster  ----------
# Für per Überweisung bezahlte Rechnungen, deren Zahlung Wochen NACH dem
# Rechnungsdatum verbucht wird (verfehlt das enge ±7-Tage-Fenster von Tier 1).


def test_auto_match_tier2_unique_amount_wide_date(tmp_path) -> None:
    """Krummer Cent-Betrag, GENAU eine offene Ausgabe mit diesem Betrag im weiten
    Fenster (Zahlung 40 Tage NACH Rechnungsdatum) → automatisch verknüpft, auch wenn
    der Banktext nur eine IBAN ist (kein Händlername nötig bei Cent-Beträgen)."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Beispielshop.pdf", "Galaxus\nTotal CHF 464.27")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            tx = Transaction(account_id=acc, date=date(2026, 4, 10), amount=Decimal("-464.27"),
                             description="E-Banking Auftrag an CH99 0900 0000")
            db.add(tx)
            db.commit()
            tx_id = tx.id
        with SessionLocal() as db:
            assert auto_match(db) == 1
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))
            assert att is not None and att.original_name == "rmatch_2026-03-01_Beispielshop.pdf"
    finally:
        settings.receipts_dir = old


def test_auto_match_tier2_skips_round_amount_without_merchant(tmp_path) -> None:
    """RUNDER Betrag (X.00, kollisionsanfällig) ohne Händler-Treffer im Banktext →
    NICHT automatisch zuordnen (zu unsicher). Bleibt für die manuelle Suche."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Spende.pdf", "Hilfswerk\nTotal CHF 333.00")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 4, 10), amount=Decimal("-333.00"),
                               description="E-Banking Auftrag an CH42 0000 1111"))
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 0
            assert len(unassigned_receipts(db)) == 1
    finally:
        settings.receipts_dir = old


def test_auto_match_tier2_round_amount_with_merchant_matches(tmp_path) -> None:
    """RUNDER Betrag wird zugeordnet, WENN der Händler (aus dem Dateinamen) im Banktext
    steht — dann ist es trotz runder Zahl eindeutig genug."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Fitnesspark.pdf", "Fitnesspark\nTotal CHF 762.00")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            tx = Transaction(account_id=acc, date=date(2026, 4, 10), amount=Decimal("-762.00"),
                             description="E-Banking FITNESSPARK AG")
            db.add(tx)
            db.commit()
            tx_id = tx.id
        with SessionLocal() as db:
            assert auto_match(db) == 1
        with SessionLocal() as db:
            assert db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id)) is not None
    finally:
        settings.receipts_dir = old


def test_auto_match_tier2_skips_two_in_window(tmp_path) -> None:
    """ZWEI offene Buchungen mit dem Betrag im weiten Fenster → mehrdeutig, keine
    automatische Zuordnung (bleibt für die manuelle Suche)."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Zweitshop.pdf", "Digitec\nTotal CHF 555.19")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add_all([
                Transaction(account_id=acc, date=date(2026, 3, 20), amount=Decimal("-555.19"),
                            description="E-Banking A"),
                Transaction(account_id=acc, date=date(2026, 4, 5), amount=Decimal("-555.19"),
                            description="E-Banking B"),
            ])
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 0
            assert len(unassigned_receipts(db)) == 1
    finally:
        settings.receipts_dir = old


def test_auto_match_tier2_respects_window_boundary(tmp_path) -> None:
    """Einzige betragsgleiche Buchung liegt WEIT ausserhalb des Fensters (>90 Tage) →
    keine automatische Zuordnung (Schutz vor Zufallstreffern aus ganz anderer Zeit)."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Brack.pdf", "Brack\nTotal CHF 624.83")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 10, 1), amount=Decimal("-624.83"),
                               description="E-Banking weit weg"))
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 0
            assert len(unassigned_receipts(db)) == 1
    finally:
        settings.receipts_dir = old


def test_auto_match_tier2_skips_transfer_leg(tmp_path) -> None:
    """Ein Kaufbeleg darf NIE an eine betragsgleiche Umbuchung/Bargeldbezug gehängt
    werden — Umbuchungen (management_type=TRANSFER) sind aus den Kandidaten raus."""
    from moneten.db.models import ManagementType

    _text_pdf(tmp_path / "rmatch_2026-03-01_Kauf.pdf", "Laden\nTotal CHF 471.65")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 3, 25), amount=Decimal("-471.65"),
                               description="Bargeldbezug Bancomat",
                               management_type=ManagementType.TRANSFER))
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 0  # Umbuchung ist kein gültiger Kandidat
            assert len(unassigned_receipts(db)) == 1
    finally:
        settings.receipts_dir = old


def test_auto_match_tier1_beats_tier2(tmp_path) -> None:
    """Liegt eine betragsgleiche Buchung im engen ±7-Tage-Fenster UND eine weitere im
    weiten Fenster, gewinnt der nähere Tier-1-Treffer (richtiges Datum)."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Ochsner.pdf", "Ochsner\nTotal CHF 318.44")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            near = Transaction(account_id=acc, date=date(2026, 3, 3), amount=Decimal("-318.44"),
                               description="Ochsner nah")  # 2 Tage → Tier 1
            far = Transaction(account_id=acc, date=date(2026, 4, 20), amount=Decimal("-318.44"),
                              description="etwas ganz anderes")  # 50 Tage → nur Tier 2
            db.add_all([near, far])
            db.commit()
            near_id = near.id
        with SessionLocal() as db:
            assert auto_match(db) == 1
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(
                Attachment.original_name == "rmatch_2026-03-01_Ochsner.pdf"))
            assert att is not None and att.transaction_id == near_id  # der nahe gewinnt
    finally:
        settings.receipts_dir = old


def test_unique_amount_winner_corroboration() -> None:
    """Reine Tier-2-Logik: krummer Cent-Betrag verknüpft nur aus ZUVERLÄSSIGER Quelle
    (Text-Layer); runder ODER OCR-Betrag nur MIT Händler-Treffer. Datum ist Pflicht,
    Mehrdeutigkeit blockt. Direkt getestet — kann nicht aus dem falschen Grund grün sein."""
    from datetime import timedelta

    from moneten.services.receipt_match import MatchCandidate, _unique_amount_winner

    base = date(2026, 3, 1)

    def cand(amount_str: str, *, merchant: bool, days: int = 10) -> MatchCandidate:
        tx = Transaction(account_id=1, date=base + timedelta(days=days),
                         amount=Decimal(amount_str), description="x")
        return MatchCandidate(tx, score=3, amount_match=True, date_match=False, merchant_match=merchant)

    # krummer Cent-Betrag, zuverlässig, ohne Händler → Treffer
    assert _unique_amount_winner(Decimal("464.20"), base, [cand("-464.20", merchant=False)],
                                 reliable_amount=True) is not None
    # krummer Cent-Betrag, NICHT zuverlässig (OCR), ohne Händler → KEIN Treffer
    assert _unique_amount_winner(Decimal("464.20"), base, [cand("-464.20", merchant=False)],
                                 reliable_amount=False) is None
    # runder Betrag, zuverlässig, ohne Händler → KEIN Treffer
    assert _unique_amount_winner(Decimal("100.00"), base, [cand("-100.00", merchant=False)],
                                 reliable_amount=True) is None
    # runder Betrag MIT Händler-Treffer → Treffer
    assert _unique_amount_winner(Decimal("100.00"), base, [cand("-100.00", merchant=True)],
                                 reliable_amount=False) is not None
    # zwei im Fenster → mehrdeutig, kein Treffer
    assert _unique_amount_winner(
        Decimal("464.20"), base,
        [cand("-464.20", merchant=False), cand("-464.20", merchant=False, days=20)],
        reliable_amount=True) is None
    # kein Datum → kein Treffer
    assert _unique_amount_winner(Decimal("464.20"), None, [cand("-464.20", merchant=False)],
                                 reliable_amount=True) is None


def test_merchant_match_word_boundary(tmp_path) -> None:
    """Händler-Token trifft nur an Wortgrenzen: „aldi" matcht NICHT „RIVALDI SOLUTIONS"."""
    from moneten.services.receipt_match import _score_candidates

    acc = _account()
    with SessionLocal() as db:
        tx = Transaction(account_id=acc, date=date(2026, 3, 1), amount=Decimal("-58.77"),
                         description="RIVALDI SOLUTIONS GMBH")
        db.add(tx)
        db.commit()
        tx_id = tx.id
        scored = _score_candidates(db, Decimal("58.77"), date(2026, 3, 1), ("aldi",))
        cand = next((c for c in scored if c.transaction.id == tx_id), None)
        assert cand is not None              # Betrag passt → ist Kandidat
        assert cand.merchant_match is False  # aber „aldi" trifft „RIVALDI" nicht


def test_auto_match_tier2_skips_two_receipts_same_amount(tmp_path) -> None:
    """Zwei offene Belege mit DEMSELBEN Betrag → Tier 2 ordnet KEINEN automatisch zu
    (unklar, welcher zur einzigen Buchung gehört) — Schutz vor Reihenfolge-Zufall."""
    _text_pdf(tmp_path / "rmatch_2026-03-01_Interio.pdf", "Interio\nTotal CHF 287.33")
    _text_pdf(tmp_path / "rmatch_2026-03-02_Microspot.pdf", "Microspot\nTotal CHF 287.33")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 3, 20), amount=Decimal("-287.33"),
                               description="E-Banking Auftrag an CH55"))
            db.commit()
        with SessionLocal() as db:
            assert auto_match(db) == 0
            assert len(unassigned_receipts(db)) == 2
    finally:
        settings.receipts_dir = old


def test_match_job_steps_to_completion(tmp_path) -> None:
    """Schritt-Job: verarbeitet Beleg für Beleg und ordnet eindeutige Treffer zu."""
    from moneten.services.jobs import get_job, start_match_job, step_match_job
    _text_pdf(tmp_path / "rmatch_2026-09-10_Job.pdf", "Job\n10.09.2026\nTotal CHF 64.30")
    _text_pdf(tmp_path / "rmatch_2026-09-11_NoMatch.pdf", "X\n11.09.2026\nTotal CHF 11.11")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2026, 9, 10), amount=Decimal("-64.30"), description="Job"))
            db.commit()
        with SessionLocal() as db:
            jid = start_match_job(db)
        assert jid
        for _ in range(10):
            with SessionLocal() as db:
                j = step_match_job(db, jid)
            if j["done"]:
                break
        j = get_job(jid)
        assert j["done"] is True
        assert j["total"] == 2
        assert j["matched"] == 1  # nur der 64.30-Beleg hat einen eindeutigen Treffer
    finally:
        settings.receipts_dir = old


def test_auto_begin_and_step_routes(logged_in_client: TestClient, tmp_path) -> None:
    import re
    _text_pdf(tmp_path / "rmatch_2026-09-20_R.pdf", "R\n20.09.2026\nTotal CHF 5.55")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        r = logged_in_client.post("/import/receipts/auto/begin")
        assert r.status_code == 200
        assert 'id="match-progress"' in r.text
        m = re.search(r"job=([a-f0-9]+)", r.text)
        assert m, "Job-ID sollte im Fortschritts-Widget stehen"
        s = logged_in_client.get(f"/import/receipts/auto/step?job={m.group(1)}")
        assert s.status_code == 200
    finally:
        settings.receipts_dir = old


def test_assign_htmx_returns_green_card(logged_in_client: TestClient, tmp_path) -> None:
    """Zuordnen per HTMX → grüne Bestätigungs-Karte; Seite zeigt Filter + Vorschau."""
    _text_pdf(tmp_path / "rmatch_2026-10-05_HX.pdf", "Shop\n05.10.2026\nBetrag 9.90")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            tx = Transaction(account_id=acc, date=date(2026, 10, 5), amount=Decimal("-9.90"),
                             description="HX-Buchung")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        page = logged_in_client.get("/import/receipts")
        assert page.status_code == 200
        assert "tx-search" in page.text             # Server-Suchfeld über alle Buchungen
        assert "receipt-info" in page.text          # Hover-Vorschau-Icon
        assert "/import/receipts/archive" in page.text  # „Archivieren (kein Bankeintrag)"-Button

        resp = logged_in_client.post(
            "/import/receipts/assign",
            data={"filename": "rmatch_2026-10-05_HX.pdf", "transaction_id": str(tx_id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "assigned-ok" in resp.text and "Zugeordnet" in resp.text
    finally:
        settings.receipts_dir = old


# ----------  Alte Quittungen ohne Bankeintrag (Archivieren)  ----------
def test_auto_archive_old_before_first_bank_entry(tmp_path) -> None:
    """Belege mit Datum VOR dem ersten Bankeintrag werden automatisch archiviert,
    Belege danach bleiben offen. cutoff wird dynamisch aus der DB gelesen (das
    Schema ist über den Testlauf gemeinsam)."""
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            db.add(Transaction(account_id=acc, date=date(2022, 5, 1), amount=Decimal("-10.00"),
                               description="Erster Bankeintrag"))
            db.commit()

        with SessionLocal() as db:
            cutoff = earliest_transaction_date(db)
        assert cutoff is not None

        # Beleg klar vor cutoff (zu alt) + Beleg nach cutoff (jung genug).
        oldd = cutoff - timedelta(days=400)
        newd = cutoff + timedelta(days=10)
        _text_pdf(tmp_path / f"arch_{oldd.isoformat()}_Alt.pdf", "Alt\nTotal CHF 5.00")
        _text_pdf(tmp_path / f"arch_{newd.isoformat()}_Neu.pdf", "Neu\nTotal CHF 6.00")

        with SessionLocal() as db:
            archived = auto_archive_old(db)
        assert archived >= 1

        with SessionLocal() as db:
            names = {r.name for r in unassigned_receipts(db)}
        assert f"arch_{oldd.isoformat()}_Alt.pdf" not in names   # archiviert → weg
        assert f"arch_{newd.isoformat()}_Neu.pdf" in names        # bleibt offen
    finally:
        settings.receipts_dir = old


def test_archive_receipt_idempotent_and_hides(tmp_path) -> None:
    """Manuelles Archivieren entfernt den Beleg aus der Liste; doppeltes
    Archivieren ist gefahrlos (kein zweiter Eintrag)."""
    _text_pdf(tmp_path / "arch_manual_2024-01-09.pdf", "Beleg\nTotal CHF 7.70")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            assert "arch_manual_2024-01-09.pdf" in {r.name for r in unassigned_receipts(db)}
            assert archive_receipt(db, "arch_manual_2024-01-09.pdf") is True
            assert archive_receipt(db, "arch_manual_2024-01-09.pdf") is False  # schon archiviert
        with SessionLocal() as db:
            assert "arch_manual_2024-01-09.pdf" not in {r.name for r in unassigned_receipts(db)}
    finally:
        settings.receipts_dir = old


def test_archive_route_htmx_returns_archived_card(logged_in_client: TestClient, tmp_path) -> None:
    """POST /receipts/archive per HTMX → graue „Archiviert"-Karte; Beleg verschwindet."""
    _text_pdf(tmp_path / "rmatch_2026-11-01_NoBank.pdf", "Beleg\nTotal CHF 4.40")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            assert "rmatch_2026-11-01_NoBank.pdf" in {r.name for r in unassigned_receipts(db)}

        resp = logged_in_client.post(
            "/import/receipts/archive",
            data={"filename": "rmatch_2026-11-01_NoBank.pdf"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "assigned-ok" in resp.text and "Archiviert" in resp.text

        with SessionLocal() as db:
            assert "rmatch_2026-11-01_NoBank.pdf" not in {r.name for r in unassigned_receipts(db)}
    finally:
        settings.receipts_dir = old


def test_assistant_page_and_assign(logged_in_client: TestClient, tmp_path) -> None:
    _text_pdf(tmp_path / "rmatch_2026-05-25_SBB.pdf", "SBB\n25.05.2026\nBetrag 33.00")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        acc = _account()
        with SessionLocal() as db:
            tx = Transaction(account_id=acc, date=date(2026, 5, 25), amount=Decimal("-33.00"), description="SBB")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        page = logged_in_client.get("/import/receipts")
        assert page.status_code == 200
        assert "rmatch_2026-05-25_SBB.pdf" in page.text
        assert "Quittungen zuordnen" in page.text

        # Zuordnen bestätigen
        resp = logged_in_client.post("/import/receipts/assign",
                                     data={"filename": "rmatch_2026-05-25_SBB.pdf", "transaction_id": str(tx_id)},
                                     follow_redirects=False)
        assert resp.status_code == 303
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))
            assert att is not None
            assert att.original_name == "rmatch_2026-05-25_SBB.pdf"
    finally:
        settings.receipts_dir = old
