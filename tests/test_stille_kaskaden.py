"""Drei Löschungen, die mehr mitnahmen, als sie ankündigten.

Der gemeinsame Nenner: die Datenbank hat gehorcht, die App hat geschwiegen.
Kein Fehler, keine Meldung — nur später falsche Zahlen. Genau das ist in einer
Finanz-App die teuerste Sorte Fehler, weil man ihn erst bemerkt, wenn man ihm
nicht mehr auf die Spur kommt.

* Kategorie löschen löste still die Verknüpfung von Abos und Verlaufsreihen
  (``ON DELETE SET NULL``). Bei einer Verlaufsreihe heisst das: der Soll/Ist-
  Abgleich vergleicht gegen nichts und meldet weiter „keine Abweichung".
* „Startsaldo aus dem Auszug übernehmen" überschrieb einen von Hand gesetzten
  Startsaldo, und bei mehreren Auszügen gewann der zuletzt gelesene. Der
  Kontostand ist ``Startsaldo + Summe aller Buchungen`` — der ganze Stand
  verschob sich also, ohne dass irgendwo stand, warum.
* „Import rückgängig" kündigte N Buchungen an und nahm die Aufteilungen und
  Beleg-Zuordnungen mit — Handarbeit, die kein zweiter Import zurückbringt.

Alle Daten hier sind erfunden.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from moneten.db.models import (
    Account,
    AccountType,
    Attachment,
    BudgetInterval,
    Category,
    ImportBatch,
    ImportSource,
    ImportStatus,
    ManagementType,
    ManualSubscription,
    MetricCadence,
    MetricKind,
    MetricSeries,
    MetricUnit,
    Transaction,
    TransactionSplit,
)
from moneten.db.session import SessionLocal

IBAN_TEST = "CH8100000000000000077"


# ---------------------------------------------------------------------------
# S6 — Kategorie löschen
# ---------------------------------------------------------------------------
@pytest.fixture
def kategorie() -> int:
    with SessionLocal() as db:
        cat = Category(
            name="Kaskaden-Testkategorie", management_type=ManagementType.DAUERAUFTRAG,
            sort_order=970,
        )
        db.add(cat)
        db.commit()
        return cat.id


def _loeschen(client: TestClient, cat_id: int) -> str:
    resp = client.post(f"/categories/{cat_id}/delete")
    assert resp.status_code in (200, 303), resp.status_code
    return resp.text


def test_kategorie_mit_abo_laesst_sich_nicht_loeschen(
    logged_in_client: TestClient, kategorie: int
) -> None:
    """Das Abo behält seine Kategorie — und die Meldung sagt, wo man nachsieht.

    Vorher stand das Abo danach ohne Kategorie da. Es tauchte weiter in der
    Abo-Liste auf, fiel aber aus jeder Auswertung nach Kategorie heraus.
    """
    with SessionLocal() as db:
        abo = ManualSubscription(
            name="Kaskaden-Testabo", amount=Decimal("9.90"),
            interval=BudgetInterval.MONATLICH, category_id=kategorie,
        )
        db.add(abo)
        db.commit()
        abo_id = abo.id

    html = _loeschen(logged_in_client, kategorie)

    assert "Abo" in html, "Die Meldung nennt das Abo nicht"
    with SessionLocal() as db:
        assert db.get(Category, kategorie) is not None, "Kategorie wurde gelöscht"
        assert db.get(ManualSubscription, abo_id).category_id == kategorie, (
            "Die Verknüpfung des Abos wurde still gelöst"
        )


def test_kategorie_mit_verlaufsreihe_laesst_sich_nicht_loeschen(
    logged_in_client: TestClient, kategorie: int
) -> None:
    """Der stillste Fall: der Soll/Ist-Abgleich hätte weiter „passt" gemeldet."""
    with SessionLocal() as db:
        reihe = MetricSeries(
            slug="kaskaden-testreihe", name="Kaskaden-Testreihe", unit=MetricUnit.CHF,
            cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE, category_id=kategorie,
        )
        db.add(reihe)
        db.commit()
        reihe_id = reihe.id

    html = _loeschen(logged_in_client, kategorie)

    assert "Verlaufsreihe" in html, "Die Meldung nennt die Verlaufsreihe nicht"
    with SessionLocal() as db:
        assert db.get(Category, kategorie) is not None
        assert db.get(MetricSeries, reihe_id).category_id == kategorie


def test_kategorie_ohne_anhaengsel_laesst_sich_weiterhin_loeschen(
    logged_in_client: TestClient, kategorie: int
) -> None:
    """Gegenprobe: die neue Sperre darf nicht alles blockieren."""
    _loeschen(logged_in_client, kategorie)
    with SessionLocal() as db:
        assert db.get(Category, kategorie) is None, "Löschen geht nicht mehr"


# ---------------------------------------------------------------------------
# S9 — Startsaldo aus dem Auszug übernehmen
# ---------------------------------------------------------------------------
def _stmt(nr: str, opening: str, von: str, bis: str, ref: str, betrag: str) -> str:
    return f"""
    <Stmt>
      <Id>{nr}</Id>
      <Acct><Id><IBAN>{IBAN_TEST}</IBAN></Id><Ccy>CHF</Ccy></Acct>
      <FrToDt><FrDtTm>{von}T00:00:00</FrDtTm><ToDtTm>{bis}T23:59:59</ToDtTm></FrToDt>
      <Bal>
        <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="CHF">{opening}</Amt><CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>{von}</Dt></Dt>
      </Bal>
      <Ntry>
        <Amt Ccy="CHF">{betrag}</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts>
        <BookgDt><Dt>{bis}</Dt></BookgDt><ValDt><Dt>{bis}</Dt></ValDt>
        <AcctSvcrRef>{ref}</AcctSvcrRef>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Erfundener Posten {ref}</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>"""


def _camt_zwei_auszuege() -> bytes:
    """Eine Datei, zwei Auszüge desselben Kontos: Anfangssaldo 100 und 900.

    Genau die Vorlage, die eine Bank für zwei Monate liefert. Vorher gewann der
    zuletzt gelesene — also der falsche.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>KASKADE-TEST</MsgId></GrpHdr>{
        _stmt("STMT-1", "100.00", "2026-06-01", "2026-06-30", "KASK-A", "10.00")
    }{
        _stmt("STMT-2", "900.00", "2026-07-01", "2026-07-31", "KASK-B", "20.00")
    }
  </BkToCstmrStmt>
</Document>
""".encode()


@pytest.fixture
def konto_mit_handsaldo() -> int:
    """Konto mit von Hand gesetztem Startsaldo 4242.00 und einer Altbuchung."""
    with SessionLocal() as db:
        acc = Account(
            name="Kaskaden-Testkonto", type=AccountType.BANK, currency="CHF",
            iban=IBAN_TEST, opening_balance=Decimal("4242.00"),
            current_balance=Decimal("4242.00"), sort_order=941,
        )
        db.add(acc)
        db.flush()
        # Liegt VOR dem Beginn des ersten Auszugs — der Anfangssaldo des Auszugs
        # enthält sie also schon.
        db.add(Transaction(
            account_id=acc.id, date=date(2026, 5, 15), amount=Decimal("-33.00"),
            description="Erfundene Altbuchung", dedup_hash="kask-alt-1",
        ))
        db.commit()
        return acc.id


def test_startsaldo_kommt_nur_aus_dem_ersten_auszug(
    logged_in_client: TestClient, konto_mit_handsaldo: int
) -> None:
    """Zwei Auszüge, ein Startsaldo — und zwar der des ersten.

    Vorher stand danach 900.00 im Konto: der Anfangssaldo des JÜNGEREN Auszugs,
    obwohl die Buchungen des älteren mitgezählt werden. Der Kontostand war damit
    um 800 zu hoch, ohne dass es irgendwo stand.
    """
    resp = logged_in_client.post(
        "/import",
        files=[("files", ("camt_erfunden.xml", _camt_zwei_auszuege(), "application/xml"))],
        data={"adopt_opening": "1"},
    )
    assert resp.status_code == 200, resp.status_code

    with SessionLocal() as db:
        acc = db.get(Account, konto_mit_handsaldo)
        assert acc.opening_balance == Decimal("100.00"), (
            f"Startsaldo ist {acc.opening_balance} statt 100.00 (erster Auszug)"
        )

    # Und die Änderung steht im Bericht, mit dem alten Wert daneben.
    assert "Startsaldo" in resp.text
    assert "4242.00" in resp.text, "Der überschriebene Wert wird nicht genannt"
    assert "unverändert" in resp.text, "Der zweite Auszug wird nicht als übersprungen gemeldet"


def test_startsaldo_uebernahme_warnt_bei_buchungen_davor(
    logged_in_client: TestClient, konto_mit_handsaldo: int
) -> None:
    """Eine Buchung vor dem Auszugsbeginn wird doppelt gezählt — das muss dastehen.

    Der Anfangssaldo des Auszugs enthält alles Vorherige bereits; die App addiert
    die alte Buchung trotzdem noch einmal dazu. Rechnerisch nicht zu verhindern
    (die Altbuchung ist echt), aber der Nutzer muss es erfahren.
    """
    resp = logged_in_client.post(
        "/import",
        files=[("files", ("camt_erfunden.xml", _camt_zwei_auszuege(), "application/xml"))],
        data={"adopt_opening": "1"},
    )
    assert resp.status_code == 200
    assert "VOR dem Auszugsbeginn" in resp.text, "Keine Warnung zur Altbuchung"


def test_ohne_haekchen_bleibt_der_startsaldo_unangetastet(
    logged_in_client: TestClient, konto_mit_handsaldo: int
) -> None:
    """Gegenprobe: ohne das Kästchen darf sich nichts am Startsaldo ändern."""
    logged_in_client.post(
        "/import",
        files=[("files", ("camt_erfunden.xml", _camt_zwei_auszuege(), "application/xml"))],
    )
    with SessionLocal() as db:
        assert db.get(Account, konto_mit_handsaldo).opening_balance == Decimal("4242.00")


# ---------------------------------------------------------------------------
# S10 — Import rückgängig machen
# ---------------------------------------------------------------------------
def test_abfrage_nennt_aufteilungen_und_belege(logged_in_client: TestClient) -> None:
    """Die Sicherheitsabfrage muss alles nennen, was mitgelöscht wird.

    Vorher stand dort „1 Buchung wird gelöscht" — und weg waren zusätzlich die
    von Hand angelegte Aufteilung und die Beleg-Zuordnung samt gelesenem Inhalt.
    """
    with SessionLocal() as db:
        acc = Account(
            name="Kaskaden-Belegkonto", type=AccountType.BANK, currency="CHF",
            opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=942,
        )
        db.add(acc)
        db.flush()
        batch = ImportBatch(
            source=ImportSource.CAMT053, filename="kaskade_erfunden.xml", account_id=acc.id,
            status=ImportStatus.COMPLETED, total_transactions=1,
        )
        db.add(batch)
        db.flush()
        tx = Transaction(
            account_id=acc.id, date=date(2026, 7, 10), amount=Decimal("-80.00"),
            description="Erfundener Einkauf", import_batch_id=batch.id, dedup_hash="kask-s10-1",
        )
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, amount=Decimal("-80.00"), category_id=None))
        db.add(Attachment(transaction_id=tx.id, original_name="beleg_erfunden.pdf"))
        db.commit()
        batch_id, tx_id = batch.id, tx.id

    html = logged_in_client.get("/import").text
    zeile = next(
        (z for z in html.splitlines() if f"/import/{batch_id}/delete" in z or "data-confirm" in z
         and "kaskade_erfunden" in z),
        "",
    )
    # Der Abfragetext steht im data-confirm des Formulars zu diesem Import.
    abschnitt = html.split(f"/import/{batch_id}/delete", 1)[1][:600] if zeile == "" else html
    assert "Aufteilung" in abschnitt, "Die Abfrage nennt die Aufteilung nicht"
    assert "Beleg-Zuordnung" in abschnitt, "Die Abfrage nennt die Beleg-Zuordnung nicht"

    # Und sie sagt die Wahrheit: beides ist danach wirklich weg.
    logged_in_client.post(f"/import/{batch_id}/delete")
    with SessionLocal() as db:
        assert db.get(Transaction, tx_id) is None
        assert db.scalar(
            TransactionSplit.__table__.select().where(TransactionSplit.transaction_id == tx_id)
        ) is None
        assert db.scalar(
            Attachment.__table__.select().where(Attachment.transaction_id == tx_id)
        ) is None
