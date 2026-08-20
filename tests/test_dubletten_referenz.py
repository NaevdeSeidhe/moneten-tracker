"""Die Duplikat-Erkennung verschluckte echte Buchungen.

Der Schlüssel war ``Datum | Betrag | erste 50 Zeichen Text``. Zwei Fälle gingen
darin verloren, beide in Richtung „zu wenig Buchungen" — also in die Richtung, in
der der Kontostand nicht mehr stimmt und man dem Fehler nicht auf die Spur kommt,
weil die fehlende Buchung nirgends auftaucht:

1. Zwei verschiedene Buchungen, deren Text sich erst **ab Zeichen 51**
   unterscheidet. Bei einem Dauerauftrag mit langer Referenz ist das der Normalfall.
2. Zwei **wirklich gleiche** Buchungen am selben Tag: zweimal 4.50 im selben
   Laden. Inhaltlich nicht zu unterscheiden, für die Kasse zwei Vorgänge. Ein
   Inhalts-Hash kann diesen Fall grundsätzlich nicht lösen — dafür braucht es die
   Nummer, die die Bank selbst vergeben hat (``AcctSvcrRef``). Der Leser hat sie
   gelesen und weggeworfen.

Was hier NICHT passieren darf: dass ein bereits importierter Auszug beim nächsten
Import komplett doppelt hereinkommt, weil sich der Schlüssel geändert hat. Der
letzte Test hält genau das fest.

Alle Daten hier sind erfunden.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal
from moneten.services.camt053_parser import make_dedup_hash

IBAN_DUB = "CH8100000000000000078"

# Ein Text, der erst weit hinten unterschiedlich wird. Die alte Erkennung sah
# davon nur die ersten 50 Zeichen — und die sind hier identisch.
LANGER_TEXT = "Dauerauftrag an erfundenen Empfaenger, Referenz Nr. "


def _ntry(betrag: str, ref: str | None, text: str, tag: str = "2026-07-14") -> str:
    """Eine Buchungszeile; ``ref=None`` lässt die Bank-Referenz weg."""
    refzeile = f"<AcctSvcrRef>{ref}</AcctSvcrRef>" if ref else ""
    return f"""
      <Ntry>
        <Amt Ccy="CHF">{betrag}</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts>
        <BookgDt><Dt>{tag}</Dt></BookgDt><ValDt><Dt>{tag}</Dt></ValDt>
        {refzeile}
        <NtryDtls><TxDtls><RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>"""


def _camt(*ntrys: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>DUBLETTE-TEST</MsgId></GrpHdr>
    <Stmt>
      <Id>STMT-DUB</Id>
      <Acct><Id><IBAN>{IBAN_DUB}</IBAN></Id><Ccy>CHF</Ccy></Acct>
      <FrToDt>
        <FrDtTm>2026-07-01T00:00:00</FrDtTm><ToDtTm>2026-07-31T23:59:59</ToDtTm>
      </FrToDt>{"".join(ntrys)}
    </Stmt>
  </BkToCstmrStmt>
</Document>
""".encode()


@pytest.fixture
def konto() -> int:
    with SessionLocal() as db:
        acc = Account(
            name="Dubletten-Testkonto", type=AccountType.BANK, currency="CHF",
            iban=IBAN_DUB, opening_balance=Decimal("0"), current_balance=Decimal("0"),
            sort_order=944,
        )
        db.add(acc)
        db.commit()
        return acc.id


def _anzahl(konto_id: int) -> int:
    with SessionLocal() as db:
        return db.scalar(
            select(func.count(Transaction.id)).where(Transaction.account_id == konto_id)
        ) or 0


def _importieren(client: TestClient, daten: bytes) -> None:
    resp = client.post(
        "/import", files=[("files", ("dublette_erfunden.xml", daten, "application/xml"))]
    )
    assert resp.status_code == 200, resp.status_code


def test_zwei_gleiche_buchungen_am_selben_tag_kommen_beide_rein(
    logged_in_client: TestClient, konto: int
) -> None:
    """Zweimal derselbe Betrag im selben Laden sind zwei Vorgänge.

    Vorher blieb davon einer übrig: gleiches Datum, gleicher Betrag, gleicher
    Text → gleicher Hash. Der Kontostand war danach um den Betrag zu hoch, und
    die fehlende Buchung tauchte nirgends auf. Die Bank vergibt für jeden Vorgang
    eine eigene Nummer — die entscheidet jetzt.
    """
    _importieren(logged_in_client, _camt(
        _ntry("4.50", "DUB-KAFFEE-1", "Erfundener Kiosk"),
        _ntry("4.50", "DUB-KAFFEE-2", "Erfundener Kiosk"),
    ))
    assert _anzahl(konto) == 2, "Eine der beiden Buchungen wurde als Dublette verworfen"


def test_unterschied_erst_ab_zeichen_51_wird_erkannt(
    logged_in_client: TestClient, konto: int
) -> None:
    """Der Hash sah nur die ersten 50 Zeichen — hier sind die identisch."""
    assert LANGER_TEXT[:50] == (LANGER_TEXT + "999")[:50], "Testtext trifft den Fall nicht"
    _importieren(logged_in_client, _camt(
        _ntry("120.00", "DUB-LANG-1", LANGER_TEXT + "111"),
        _ntry("120.00", "DUB-LANG-2", LANGER_TEXT + "222"),
    ))
    assert _anzahl(konto) == 2, "Die zweite Buchung wurde am Textanfang als Dublette verworfen"


def test_dieselbe_referenz_bleibt_eine_dublette(
    logged_in_client: TestClient, konto: int
) -> None:
    """Der eigentliche Zweck bleibt erhalten: derselbe Auszug, zweimal importiert."""
    daten = _camt(
        _ntry("77.00", "DUB-EINMAL", "Erfundene Zahlung"),
        _ntry("12.00", "DUB-ZWEIMAL", "Erfundene andere Zahlung"),
    )
    _importieren(logged_in_client, daten)
    assert _anzahl(konto) == 2
    _importieren(logged_in_client, daten)
    assert _anzahl(konto) == 2, "Der zweite Import hat Dubletten erzeugt"


def test_referenz_wird_gespeichert(logged_in_client: TestClient, konto: int) -> None:
    """Ohne gespeicherte Referenz wäre der Schlüssel beim nächsten Mal wieder weg."""
    _importieren(logged_in_client, _camt(_ntry("55.00", "DUB-GEMERKT", "Erfundener Posten")))
    with SessionLocal() as db:
        tx = db.scalars(select(Transaction).where(Transaction.account_id == konto)).one()
        assert tx.bank_reference == "DUB-GEMERKT"
        assert tx.dedup_hash, "Der Inhalts-Hash muss weiterhin gesetzt werden (CSV-Rückfall)"


def test_altbestand_ohne_referenz_kommt_nicht_doppelt_herein(
    logged_in_client: TestClient, konto: int
) -> None:
    """Der entscheidende Test für die Umstellung.

    Buchungen aus der Zeit vor der neuen Spalte haben keine Referenz. Würde der
    Import nur noch die Referenz vergleichen, käme beim nächsten Auszug der ganze
    Altbestand doppelt herein — bei einem Jahr Historie hunderte Dubletten, und
    jede Auswertung wäre doppelt so hoch.
    """
    with SessionLocal() as db:
        # Genau so sieht eine vor Migration 0030 importierte Buchung aus.
        db.add(Transaction(
            account_id=konto, date=date(2026, 7, 14), amount=Decimal("-99.00"),
            description="Erfundener Altposten",
            dedup_hash=make_dedup_hash(date(2026, 7, 14), Decimal("-99.00"), "Erfundener Altposten"),
            bank_reference=None,
        ))
        db.commit()

    _importieren(logged_in_client, _camt(
        _ntry("99.00", "DUB-ALT-JETZT-MIT-REF", "Erfundener Altposten"),
    ))
    assert _anzahl(konto) == 1, "Der Altbestand wurde doppelt importiert"


def test_csv_ohne_referenz_dedupliziert_weiter_ueber_den_inhalt(
    logged_in_client: TestClient, konto: int
) -> None:
    """Eine CSV trägt keine Bank-Referenz — dort muss der alte Weg weiter greifen."""
    csv = (
        b"Datum;Buchungstext;Betrag\n"
        b"14.07.2026;Erfundener CSV-Posten;-31.00\n"
    )
    for _ in range(2):
        resp = logged_in_client.post(
            "/import",
            files=[("files", ("erfunden.csv", csv, "text/csv"))],
            data={"account_id": str(konto)},
        )
        assert resp.status_code == 200
    assert _anzahl(konto) == 1, "Der zweite CSV-Import hat eine Dublette erzeugt"
