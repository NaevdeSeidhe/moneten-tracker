"""Vertrag zwischen dem Doppelklick-Starter und der Import-Seite.

``Moneten-Import.ps1`` laedt den CAMT.053-Auszug ueber dieselbe Route hoch wie
das Formular und liest danach die HTML-Antwort aus, um zu melden, wie viele
Buchungen neu sind. Diese Kopplung ist unsichtbar: wer ``import.html`` umbaut,
merkt nichts davon — der Starter meldete dann still „keinen Bericht" oder,
schlimmer, eine falsche Zahl.

Darum stehen die Muster hier EINMAL und werden zweimal geprueft:

1. gegen die echte Antwort der Route, und zwar je in dem Zustand, der die
   Meldung ueberhaupt erzeugt (Treffer, Duplikat, kaputte Datei, IBAN ohne
   Konto, Saldo-Differenz, fehlender Schlusssaldo),
2. gegen den Text des Skripts — das Skript muss genau diese Muster benutzen.

Faellt einer der beiden Tests, ist die Antwort nicht „Test anpassen", sondern
„Skript und Template wieder in Deckung bringen".

**Drei der Tests werden in einem Klon übersprungen.** ``Moneten-Import.ps1``
ist ein Doppelklick-Starter aus dem Arbeitsordner des Autors und wird nicht
mitgeliefert. Die übrigen Tests laufen: sie prüfen die Muster gegen die echte
Antwort der ``/import``-Route, und die ist mit dabei.

Alle Daten hier sind erfunden.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moneten.db.models import Account, AccountType
from moneten.db.session import SessionLocal

# Der Starter liegt NEBEN dem Projektordner, bei den anderen Doppelklick-Startern.
STARTER = Path(__file__).resolve().parents[2] / "Moneten-Import.ps1"
STARTER_CMD = Path(__file__).resolve().parents[2] / "Moneten-Import.cmd"

# --- Die Muster, die der Starter auf die Antwortseite anwendet --------------
MUSTER = {
    "dateien": r"(\d+)\s*Datei\(en\)",
    "auszuege": r"(\d+)\s*Auszug",
    "neu_gesamt": r"<strong[^>]*>\s*(\d+)\s*</strong>\s*importiert",
    "dup_gesamt": r"(\d+)\s*\S*bersprungen",
    "block_neu": r"\+(\d+)\s*neu",
    "block_dup": r"(\d+)\s*Duplikate",
    "block_name": r'<div class="label-cap"[^>]*>\s*([^<]+)',
    "ohne_konto": r"Kein Konto gefunden f\S*r IBAN:\s*([^<]+)",
    "geht_nicht_auf": r"geht nicht auf:\s*([^<]+)",
    "datei_fehler": r'(?s)<div class="feedback err"[^>]*>\s*(<div>.*?</div>)\s*</div>',
}

# Feste Zeichenketten, an denen der Starter zerlegt bzw. Zustaende erkennt.
MARKEN = [
    '<div class="card sunken"',   # Trenner: ein Block je Auszug
    '<div class="feedback err"',  # Fehlerbanner (auch ohne Bericht)
    "Saldo-Differenz",
    "Kein Schluss-Saldo",
]

IBAN_OK = "CH8100000000000000042"
IBAN_FREMD = "CH8100000000000000043"


def _camt(iban: str, *, closing: str | None = "4140.00", opening: str = "0.00") -> bytes:
    """Erfundener Auszug: -60.00 und +4200.00, also Summe 4140.00.

    ``closing=None`` laesst den Schlusssaldo weg (manche Institute liefern ihn
    nicht), ein abweichender ``closing`` laesst die Datei nicht aufgehen.
    """
    clbd = "" if closing is None else f"""
      <Bal>
        <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="CHF">{closing}</Amt><CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-07-31</Dt></Dt>
      </Bal>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>STARTER-TEST</MsgId></GrpHdr>
    <Stmt>
      <Id>STMT-STARTER</Id>
      <Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>CHF</Ccy></Acct>
      <FrToDt>
        <FrDtTm>2026-07-01T00:00:00</FrDtTm>
        <ToDtTm>2026-07-31T23:59:59</ToDtTm>
      </FrToDt>
      <Bal>
        <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="CHF">{opening}</Amt><CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-07-01</Dt></Dt>
      </Bal>{clbd}
      <Ntry>
        <Amt Ccy="CHF">60.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts>
        <BookgDt><Dt>2026-07-05</Dt></BookgDt><ValDt><Dt>2026-07-05</Dt></ValDt>
        <AcctSvcrRef>STARTER-A</AcctSvcrRef>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Testladen Musterhausen</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="CHF">4200.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>BOOK</Sts>
        <BookgDt><Dt>2026-07-25</Dt></BookgDt><ValDt><Dt>2026-07-25</Dt></ValDt>
        <AcctSvcrRef>STARTER-B</AcctSvcrRef>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Gutschrift Musterfirma AG</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
""".encode()


def _wert(html: str, schluessel: str) -> str:
    """Wendet ein Starter-Muster an und liefert dessen Fanggruppe."""
    m = re.search(MUSTER[schluessel], html)
    assert m is not None, f"Muster '{schluessel}' trifft die Import-Antwort nicht mehr"
    return m.group(1)


def _hochladen(client: TestClient, daten: bytes, name: str = "camt053_erfunden.xml") -> str:
    resp = client.post("/import", files=[("files", (name, daten, "application/xml"))])
    assert resp.status_code in (200, 400), resp.status_code
    return resp.text


@pytest.fixture
def konto() -> int:
    """Erfundenes Konto mit der IBAN des Testauszugs."""
    with SessionLocal() as db:
        acc = Account(
            name="Starter-Testkonto", type=AccountType.BANK, currency="CHF",
            iban=IBAN_OK, opening_balance=Decimal("0"), current_balance=Decimal("0"),
            sort_order=940,
        )
        db.add(acc)
        db.commit()
        return acc.id


def test_erster_import_liefert_die_zahlen_die_der_starter_meldet(
    logged_in_client: TestClient, konto: int
) -> None:
    """Der Normalfall: der Starter muss 2 neue Buchungen auf einem Konto melden."""
    html = _hochladen(logged_in_client, _camt(IBAN_OK))

    assert _wert(html, "dateien") == "1"
    assert _wert(html, "auszuege") == "1"
    assert _wert(html, "neu_gesamt") == "2"
    assert _wert(html, "dup_gesamt") == "0"
    assert _wert(html, "block_neu") == "2"
    assert _wert(html, "block_dup") == "0"
    assert _wert(html, "block_name").strip().startswith("Starter-Testkonto")

    # Ein Block je Auszug — daran zerlegt der Starter den Bericht.
    assert html.count('<div class="card sunken"') == 1
    # Der Auszug geht auf: keine Warnung. Sonst warnte der Starter immer, und
    # man gewoehnte sich das Wegschauen an, bis die Warnung einmal stimmt.
    assert "Saldo-Differenz" not in html
    assert "Kein Schluss-Saldo" not in html
    assert "Saldo stimmt" in html


def test_zweiter_import_meldet_duplikate(logged_in_client: TestClient, konto: int) -> None:
    """Derselbe Auszug nochmal: 0 neu, 2 uebersprungen — der Starter sagt
    „nichts Neues" statt still Erfolg zu melden."""
    _hochladen(logged_in_client, _camt(IBAN_OK))
    html = _hochladen(logged_in_client, _camt(IBAN_OK))

    assert _wert(html, "neu_gesamt") == "0"
    assert _wert(html, "dup_gesamt") == "2"
    assert _wert(html, "block_neu") == "0"
    assert _wert(html, "block_dup") == "2"


def test_unbekannte_iban_wird_gemeldet(logged_in_client: TestClient, konto: int) -> None:
    """Ein Auszug ohne passendes Konto darf nicht als „0 neu" durchgehen."""
    html = _hochladen(logged_in_client, _camt(IBAN_FREMD))
    assert IBAN_FREMD in _wert(html, "ohne_konto")


def test_kaputte_datei_wird_gemeldet(logged_in_client: TestClient) -> None:
    """Eine unlesbare Datei ergibt einen Bericht mit 0 neu — ohne diesen Fehler-
    Abschnitt haette der Starter das als „nichts Neues" verkauft."""
    html = _hochladen(logged_in_client, b"<Document>kaputt</Document>", "camt053_kaputt.xml")
    assert _wert(html, "neu_gesamt") == "0"
    assert "camt053_kaputt.xml" in _wert(html, "datei_fehler")


def test_ohne_datei_kommt_kein_bericht(logged_in_client: TestClient) -> None:
    """Der Zweig, in dem der Starter abbricht: kein Bericht, dafuer ein Banner."""
    resp = logged_in_client.post("/import", data={"account_id": ""})
    assert resp.status_code == 400
    assert re.search(MUSTER["neu_gesamt"], resp.text) is None
    assert '<div class="feedback err"' in resp.text


def test_datei_die_nicht_aufgeht(logged_in_client: TestClient, konto: int) -> None:
    """Falscher Schlusssaldo: die Datei geht in sich nicht auf."""
    html = _hochladen(logged_in_client, _camt(IBAN_OK, closing="9999.00"))
    assert "Saldo-Differenz" in html
    assert _wert(html, "geht_nicht_auf").strip()


def test_ohne_schlusssaldo_kein_abgleich(logged_in_client: TestClient, konto: int) -> None:
    """Ohne CLBD gibt es kein Urteil — der Starter meldet das, statt zu schweigen."""
    html = _hochladen(logged_in_client, _camt(IBAN_OK, closing=None))
    assert "Kein Schluss-Saldo" in html


def test_starter_benutzt_genau_diese_muster() -> None:
    """Die Muster oben stehen wortgleich im Skript.

    Ohne diese Klammer prueften die Tests oben ein Template gegen sich selbst,
    waehrend das Skript laengst etwas anderes sucht.
    """
    if not STARTER.exists():
        pytest.skip(
            "Moneten-Import.ps1 ist ein Doppelklick-Starter aus dem Arbeitsordner "
            "des Autors und wird nicht mitgeliefert."
        )
    text = STARTER.read_text(encoding="ascii")

    for name, muster in MUSTER.items():
        assert muster in text, f"Muster '{name}' steht so nicht (mehr) in {STARTER.name}"
    for marke in MARKEN:
        assert marke in text, f"Marke {marke!r} steht so nicht (mehr) in {STARTER.name}"


def test_starter_ist_ascii_und_haengt_am_cmd() -> None:
    """PowerShell 5.1 liest .ps1 ohne BOM als ANSI — Umlaute wuerden zu Salat.

    Und das .cmd muss die .ps1 gleich aufrufen wie die uebrigen Starter, sonst
    startet der Doppelklick nichts.
    """
    if not STARTER.exists():
        pytest.skip(
            "Moneten-Import.ps1 ist ein Doppelklick-Starter aus dem Arbeitsordner "
            "des Autors und wird nicht mitgeliefert."
        )
    assert max(STARTER.read_bytes()) < 128, "Moneten-Import.ps1 enthaelt Zeichen ausserhalb ASCII"

    assert STARTER_CMD.exists(), "Moneten-Import.cmd fehlt — kein Doppelklick-Start"
    cmd = STARTER_CMD.read_text(encoding="ascii")
    assert "-NoProfile -ExecutionPolicy Bypass -File" in cmd
    assert "Moneten-Import.ps1" in cmd


def test_starter_enthaelt_keine_pin() -> None:
    """Ein Klartext-Geheimnis in einer Doppelklick-Datei waere der Punkt, an dem
    die PIN aufhoert, eine zu sein."""
    if not STARTER.exists():
        pytest.skip(
            "Moneten-Import.ps1 ist ein Doppelklick-Starter aus dem Arbeitsordner "
            "des Autors und wird nicht mitgeliefert."
        )
    text = STARTER.read_text(encoding="ascii")
    # Zuweisung einer 4- bis 8-stelligen Zahl an irgendetwas PIN-artiges.
    assert not re.search(r"(?i)\$?\w*pin\w*\s*=\s*[\"']?\d{4,8}", text)
    # Abgefragt wird zur Laufzeit, und nicht im Klartext auf dem Schirm.
    assert "-AsSecureString" in text
