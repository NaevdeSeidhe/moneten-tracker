"""Tests für den CAMT.053-Parser und den Import-Flow (Phase 1).

Nutzt eine eingebettete Beispiel-XML im ISO-20022-Format (camt.053.001.08).
Sobald eine echte Bankdatei vorliegt, wird hier ein zweiter Fall ergänzt.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal
from moneten.services.camt053_parser import make_dedup_hash, parse_camt053, parse_camt053_all

# Beispiel: Opening 1000.00, Lohn +5212.00, Migros -55.40, Miete -1350.00
# → Closing 4806.60
SAMPLE_CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>TEST-MSG</MsgId></GrpHdr>
    <Stmt>
      <Id>STMT-1</Id>
      <Acct>
        <Id><IBAN>CH9300762011623852957</IBAN></Id>
        <Ccy>CHF</Ccy>
      </Acct>
      <FrToDt>
        <FrDtTm>2026-05-01T00:00:00</FrDtTm>
        <ToDtTm>2026-05-31T23:59:59</ToDtTm>
      </FrToDt>
      <Bal>
        <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="CHF">1000.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-05-01</Dt></Dt>
      </Bal>
      <Bal>
        <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="CHF">4806.60</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-05-31</Dt></Dt>
      </Bal>
      <Ntry>
        <Amt Ccy="CHF">5212.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-05-25</Dt></BookgDt>
        <ValDt><Dt>2026-05-25</Dt></ValDt>
        <AcctSvcrRef>REF-LOHN</AcctSvcrRef>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Lohn Mai Arbeitgeber AG</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="CHF">55.40</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-05-15</Dt></BookgDt>
        <ValDt><Dt>2026-05-15</Dt></ValDt>
        <AcctSvcrRef>REF-MIGROS</AcctSvcrRef>
        <AddtlNtryInf>MIGROS MM</AddtlNtryInf>
      </Ntry>
      <Ntry>
        <Amt Ccy="CHF">1350.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-05-01</Dt></BookgDt>
        <ValDt><Dt>2026-05-01</Dt></ValDt>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Miete Mai</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_basic_fields() -> None:
    stmt = parse_camt053(SAMPLE_CAMT)
    assert stmt.iban == "CH9300762011623852957"
    assert stmt.currency == "CHF"
    assert stmt.period_from == date(2026, 5, 1)
    assert stmt.period_to == date(2026, 5, 31)
    assert stmt.opening_balance == Decimal("1000.00")
    assert stmt.closing_balance == Decimal("4806.60")


def test_parse_entries_signs() -> None:
    stmt = parse_camt053(SAMPLE_CAMT)
    assert len(stmt.entries) == 3
    by_amount = {e.amount for e in stmt.entries}
    assert Decimal("5212.00") in by_amount    # Gutschrift positiv
    assert Decimal("-55.40") in by_amount     # Belastung negativ
    assert Decimal("-1350.00") in by_amount
    # Summe der Buchungen + opening == closing
    total = sum(e.amount for e in stmt.entries)
    assert stmt.opening_balance + total == stmt.closing_balance


def test_parse_description_fallback() -> None:
    stmt = parse_camt053(SAMPLE_CAMT)
    descs = {e.description for e in stmt.entries}
    assert "Lohn Mai Arbeitgeber AG" in descs   # aus RmtInf/Ustrd
    assert "MIGROS MM" in descs                # aus AddtlNtryInf (Fallback)


def test_parse_invalid_xml() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_camt053(b"<nonsense/>")


def test_dedup_hash_stable() -> None:
    h1 = make_dedup_hash(date(2026, 5, 15), Decimal("-55.40"), "MIGROS MM")
    h2 = make_dedup_hash(date(2026, 5, 15), Decimal("-55.40"), "MIGROS MM")
    h3 = make_dedup_hash(date(2026, 5, 15), Decimal("-55.41"), "MIGROS MM")
    assert h1 == h2
    assert h1 != h3


# ---------------------------------------------------------------------------
# Import-Flow
# ---------------------------------------------------------------------------


def _make_import_account() -> int:
    """Frisches Konto für isolierte Import-Tests (opening 0)."""
    with SessionLocal() as db:
        acc = Account(name="Import-Testkonto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"))
        db.add(acc)
        db.commit()
        return acc.id


def test_import_creates_transactions_and_matches_balance(logged_in_client: TestClient) -> None:
    acc_id = _make_import_account()
    resp = logged_in_client.post(
        "/import",
        data={"account_id": str(acc_id), "adopt_opening": "1"},
        files={"file": ("auszug.xml", SAMPLE_CAMT, "text/xml")},
    )
    assert resp.status_code == 200
    assert "Import abgeschlossen" in resp.text
    # 3 Buchungen importiert, Saldo stimmt überein (adopt_opening setzt opening=1000).
    assert "Saldo stimmt überein" in resp.text

    with SessionLocal() as db:
        acc = db.get(Account, acc_id)
        assert acc.current_balance == Decimal("4806.60")
        n = len(list(db.scalars(select(Transaction).where(Transaction.account_id == acc_id))))
        assert n == 3


def test_import_dedup_on_second_run(logged_in_client: TestClient) -> None:
    acc_id = _make_import_account()
    payload = {"data": {"account_id": str(acc_id), "adopt_opening": "1"},
               "files": {"file": ("auszug.xml", SAMPLE_CAMT, "text/xml")}}
    # Erster Import
    logged_in_client.post("/import", **payload)
    # Zweiter Import derselben Datei → alle als Duplikat übersprungen
    resp = logged_in_client.post("/import", **payload)
    assert resp.status_code == 200
    with SessionLocal() as db:
        n = len(list(db.scalars(select(Transaction).where(Transaction.account_id == acc_id))))
    assert n == 3  # keine Verdopplung


def test_import_rejects_garbage(logged_in_client: TestClient) -> None:
    acc_id = _make_import_account()
    resp = logged_in_client.post(
        "/import",
        data={"account_id": str(acc_id)},
        files={"file": ("kaputt.xml", b"<nope/>", "text/xml")},
    )
    assert resp.status_code == 400
    assert "konnte nicht gelesen werden" in resp.text


# ---------------------------------------------------------------------------
# Mehrere Konten in einer Datei (kombinierter Export) + IBAN-Auto-Zuordnung
# ---------------------------------------------------------------------------

# Zwei Auszüge in EINER Datei, verschiedene IBANs.
MULTI_CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>MULTI</MsgId></GrpHdr>
    <Stmt>
      <Id>A</Id>
      <Acct><Id><IBAN>CH00TESTAAA0000000001</IBAN></Id><Ccy>CHF</Ccy></Acct>
      <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">100.00</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>
      <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">150.00</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>
      <Ntry><Amt Ccy="CHF">50.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-05-10</Dt></BookgDt>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Konto A Eingang</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
    </Stmt>
    <Stmt>
      <Id>B</Id>
      <Acct><Id><IBAN>CH00TESTBBB0000000002</IBAN></Id><Ccy>CHF</Ccy></Acct>
      <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">200.00</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>
      <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">170.00</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>
      <Ntry><Amt Ccy="CHF">30.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-05-12</Dt></BookgDt>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Konto B Ausgang</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_parse_all_returns_multiple_statements() -> None:
    stmts = parse_camt053_all(MULTI_CAMT)
    assert len(stmts) == 2
    assert {s.iban for s in stmts} == {"CH00TESTAAA0000000001", "CH00TESTBBB0000000002"}


def test_import_multi_account_auto_match_by_iban(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        a = Account(name="Multi-A", type=AccountType.BANK, currency="CHF", iban="CH00TESTAAA0000000001",
                    opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=910)
        b = Account(name="Multi-B", type=AccountType.BANK, currency="CHF", iban="CH00TESTBBB0000000002",
                    opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=911)
        db.add_all([a, b])
        db.commit()
        aid, bid = a.id, b.id

    # KEIN account_id mitgeben → Zuordnung rein über IBAN.
    resp = logged_in_client.post(
        "/import",
        data={"adopt_opening": "1"},
        files={"file": ("alle_konten.xml", MULTI_CAMT, "text/xml")},
    )
    assert resp.status_code == 200

    with SessionLocal() as db:
        ta = list(db.scalars(select(Transaction).where(Transaction.account_id == aid)))
        tb = list(db.scalars(select(Transaction).where(Transaction.account_id == bid)))
    assert len(ta) == 1 and ta[0].amount == Decimal("50.00")    # Konto A → +50
    assert len(tb) == 1 and tb[0].amount == Decimal("-30.00")   # Konto B → −30


# ---------------------------------------------------------------------------
# Mehrere DATEIEN auf einmal (eine pro Konto) — Mehrfach-Upload
# ---------------------------------------------------------------------------
def _single_stmt(iban: str, op: str, cl: str, day: str, amt: str, ind: str, text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>'
        '<GrpHdr><MsgId>F</MsgId></GrpHdr><Stmt><Id>S</Id>'
        f'<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>CHF</Ccy></Acct>'
        f'<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">{op}</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>'
        f'<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">{cl}</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>'
        f'<Ntry><Amt Ccy="CHF">{amt}</Amt><CdtDbtInd>{ind}</CdtDbtInd><BookgDt><Dt>{day}</Dt></BookgDt>'
        f'<NtryDtls><TxDtls><RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>'
        '</Stmt></BkToCstmrStmt></Document>'
    ).encode()


def test_import_multiple_files_one_per_account(logged_in_client: TestClient) -> None:
    """Mehrere Dateien (eine pro Konto) in EINEM Upload — jede per IBAN zugeordnet."""
    with SessionLocal() as db:
        c = Account(name="Multi-C", type=AccountType.BANK, currency="CHF", iban="CH00TESTCCC0000000003",
                    opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=912)
        d = Account(name="Multi-D", type=AccountType.BANK, currency="CHF", iban="CH00TESTDDD0000000004",
                    opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=913)
        db.add_all([c, d])
        db.commit()
        cid, did = c.id, d.id

    file_c = _single_stmt("CH00TESTCCC0000000003", "0.00", "77.00", "2026-05-14", "77.00", "CRDT", "Konto C Eingang")
    file_d = _single_stmt("CH00TESTDDD0000000004", "0.00", "-33.00", "2026-05-15", "33.00", "DBIT", "Konto D Ausgang")

    resp = logged_in_client.post(
        "/import",
        data={"adopt_opening": "1"},
        files=[
            ("files", ("konto_c.xml", file_c, "text/xml")),
            ("files", ("konto_d.xml", file_d, "text/xml")),
        ],
    )
    assert resp.status_code == 200
    assert "2 Datei(en)" in resp.text          # Report zeigt die Dateianzahl
    assert "konto_c.xml" in resp.text and "konto_d.xml" in resp.text

    with SessionLocal() as db:
        tc = list(db.scalars(select(Transaction).where(Transaction.account_id == cid)))
        td = list(db.scalars(select(Transaction).where(Transaction.account_id == did)))
    assert len(tc) == 1 and tc[0].amount == Decimal("77.00")
    assert len(td) == 1 and td[0].amount == Decimal("-33.00")


def test_camt_mit_bom_wird_als_xml_erkannt() -> None:
    """Ein UTF-8-BOM vor der XML-Deklaration darf die Datei nicht zur CSV machen.

    ``bytes.lstrip()`` entfernt nur Leerraum — das BOM bleibt stehen, und damit
    ist das erste Byte nicht ``<``. E-Banking-Exporte liefern camt.053 durchaus
    mit BOM; die Datei landete im CSV-Zweig und wurde mit einer Begründung
    abgewiesen, die mit der Ursache nichts zu tun hatte.
    """
    from moneten.routers.import_bank import ist_xml

    bom = b"\xef\xbb\xbf"
    assert ist_xml(SAMPLE_CAMT) is True
    assert ist_xml(bom + SAMPLE_CAMT) is True, "Mit BOM gilt die Datei als CSV"
    assert ist_xml(b"  \n<Document/>") is True
    assert ist_xml(b"Datum;Text;Betrag\n01.01.2026;x;1.00") is False

