"""Tests für den CSV-Import-Fallback (Auto-Detection von Spalten/Trennzeichen)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal
from moneten.services.csv_parser import parse_csv_statements


def test_parse_signed_amount_column() -> None:
    csv = (
        b"Datum;Buchungstext;Betrag;Saldo\n"
        b"15.05.2026;Coop Musterstadt;-42.50;1'957.50\n"
        b"16.05.2026;Lohn Planikum AG;3'000.00;4'957.50\n"
    )
    [stmt] = parse_csv_statements(csv)
    assert len(stmt.entries) == 2
    assert stmt.entries[0].amount == Decimal("-42.50")
    assert stmt.entries[0].description == "Coop Musterstadt"
    assert stmt.entries[1].amount == Decimal("3000.00")
    assert stmt.period_from == date(2026, 5, 15)
    assert stmt.period_to == date(2026, 5, 16)
    assert stmt.closing_balance == Decimal("4957.50")  # Saldo der jüngsten Zeile


def test_parse_separate_debit_credit_columns() -> None:
    csv = (
        b"Datum,Text,Belastung,Gutschrift,Saldo\n"
        b"15.05.2026,Coop,42.50,,1957.50\n"
        b"16.05.2026,Lohn,,3000.00,4957.50\n"
    )
    [stmt] = parse_csv_statements(csv)
    assert stmt.entries[0].amount == Decimal("-42.50")   # Belastung → negativ
    assert stmt.entries[1].amount == Decimal("3000.00")  # Gutschrift → positiv


def test_csv_import_route_creates_transactions(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        acc = Account(name="CSV-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=930)
        db.add(acc)
        db.commit()
        acc_id = acc.id

    csv = (
        b"Datum;Buchungstext;Betrag;Saldo\n"
        b"10.04.2026;ZZZcsvshop Einkauf;-19.95;100.00\n"
        b"11.04.2026;ZZZcsvlohn;500.00;600.00\n"
    )
    resp = logged_in_client.post(
        "/import",
        files={"file": ("auszug.csv", csv, "text/csv")},
        data={"account_id": str(acc_id)},
    )
    assert resp.status_code == 200
    with SessionLocal() as db:
        n = db.scalar(
            select(func.count(Transaction.id)).where(Transaction.account_id == acc_id)
        )
        assert n == 2


# ---------------------------------------------------------------------------
# Zeilen, die der Leser nicht versteht, verschwanden lautlos
# ---------------------------------------------------------------------------
def test_unlesbare_zeilen_werden_gezaehlt() -> None:
    """Der Leser darf wegwerfen — aber nicht ohne es zu sagen.

    Die drei ``continue`` im Zeilendurchlauf sind einzeln vernünftig (Summenzeile,
    Fusszeile). Sie verschlucken aber genauso echte Buchungen mit einem
    Betragsformat, das der Leser nicht kennt. Hier: eine Zeile in Klammer-Notation
    für negative Beträge, wie sie aus einem Tabellenprogramm kommt, und eine mit
    einem Datum in US-Schreibweise. Vorher meldete der Bericht „1 importiert" und
    verlor zwei Buchungen, ohne ein Wort.
    """
    csv = (
        b"Datum;Buchungstext;Betrag;Saldo\n"
        b"15.05.2026;Erfundener Laden;-42.50;1000.00\n"
        b"16.05.2026;Erfundene Miete;(1200.00);-200.00\n"
        b"2026/05/17;Erfundener Lohn;3000.00;2800.00\n"
        b";Zwischensumme;;\n"
    )
    [stmt] = parse_csv_statements(csv)

    assert len(stmt.entries) == 1, "Der Leser versteht plötzlich mehr als gedacht"
    assert stmt.uebersprungene_zeilen == 3, (
        f"{stmt.uebersprungene_zeilen} statt 3 übersprungene Zeilen gezählt"
    )
    # Die Beispiele machen die Zeile wiedererkennbar, ohne die Datei nachzudrucken.
    assert any("Erfundene Miete" in b for b in stmt.uebersprungene_beispiele)
    assert all(len(b) <= 120 for b in stmt.uebersprungene_beispiele)


def test_saubere_datei_meldet_keine_uebersprungenen_zeilen() -> None:
    """Gegenprobe: die Warnung darf nicht bei jedem Import stehen."""
    csv = (
        b"Datum;Buchungstext;Betrag;Saldo\n"
        b"15.05.2026;Erfundener Laden;-42.50;1000.00\n"
    )
    [stmt] = parse_csv_statements(csv)
    assert stmt.uebersprungene_zeilen == 0
    assert stmt.uebersprungene_beispiele == []


def test_bericht_nennt_die_uebersprungenen_zeilen(logged_in_client: TestClient) -> None:
    """Gezählt reicht nicht — es muss auf der Seite stehen."""
    with SessionLocal() as db:
        acc = Account(
            name="CSV-Warnkonto", type=AccountType.BANK, currency="CHF",
            opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=943,
        )
        db.add(acc)
        db.commit()
        acc_id = acc.id

    csv = (
        b"Datum;Buchungstext;Betrag\n"
        b"15.05.2026;Erfundener Laden;-42.50\n"
        b"16.05.2026;Erfundene Miete;(1200.00)\n"
    )
    resp = logged_in_client.post(
        "/import",
        files=[("files", ("erfunden.csv", csv, "text/csv"))],
        data={"account_id": str(acc_id)},
    )
    assert resp.status_code == 200, resp.status_code
    assert "übersprungen" in resp.text, "Der Bericht nennt die verworfene Zeile nicht"
    assert "Erfundene Miete" in resp.text, "Kein Beispiel der verworfenen Zeile"


def test_buchungsdatum_wird_nicht_zum_buchungstext() -> None:
    """Die Kopfzeile, an der die Zuordnung zerbrach.

    ``"buchung"`` ist Stichwort für den Buchungstext und zugleich Teilstring von
    ``"Buchungsdatum"``. Weil von links gesucht wurde, gewann die Datumsspalte —
    jede Buchung hiess danach nach ihrem Datum, und die Spalte ``Text`` blieb
    ungelesen. Diese Kopfzeile liefern gängige Schweizer Bankexporte; die alten
    Tests kannten nur ``Datum;Buchungstext;…``, wo die Reihenfolge zufällig
    rettete.
    """
    from moneten.services.csv_parser import parse_csv_statements

    roh = (
        b"Buchungsdatum;Text;Betrag;Saldo\n"
        b"01.08.2026;Musterladen Einkauf;-42.50;1000.00\n"
        b"02.08.2026;Lohn Arbeitgeber AG;5000.00;6000.00\n"
    )

    eintraege = parse_csv_statements(roh)[0].entries
    texte = [e.description for e in eintraege]
    assert texte == ["Musterladen Einkauf", "Lohn Arbeitgeber AG"], texte
    assert not any("2026" in t for t in texte), (
        f"Das Datum ist als Buchungstext gelandet: {texte}"
    )
