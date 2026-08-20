"""Rückstellung des Treffen-Fonds auf dem echten Ferienkonto.

Zwei Fragen, die der Fonds vorher nicht beantworten konnte:

1. **Liegt das Geld da?** Die Monatsliste sagt „zurückgelegt" — das ist ein Klick,
   keine Überweisung. Erst der Vergleich mit den Zuflüssen auf dem Konto sagt, ob
   das Geld den Weg auch genommen hat.
2. **Was hat die Reise gekostet?** Die Formel rechnet, das Konto weiss es.

Alle Beträge und Namen sind erfunden.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    ManagementType,
    MeetContribution,
    MeetFundSettings,
    MeetVisit,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services import meet_fund

HEUTE = date(2026, 8, 11)
START = date(2026, 1, 1)


@pytest.fixture()
def fonds():
    """Frischer Fonds mit Ferienkonto — und ohne Spuren für andere Tests.

    Die Testdatenbank ist über die Datei hinweg dieselbe. ``rollback`` am Ende
    ist Pflicht, nicht Sorgfalt: :func:`rueckstellung` summiert ALLE bestätigten
    Rücklagen, auch die eines anderen Tests.
    """
    with SessionLocal() as db:
        # Bestehende Zeilen wegräumen, damit die Summen nur aus diesem Test stammen.
        for c in db.scalars(select(MeetContribution)):
            db.delete(c)
        for v in db.scalars(select(MeetVisit)):
            db.delete(v)
        konto = Account(name="Ferienkonto (Test)", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"),
                        sort_order=902)
        db.add(konto)
        db.flush()
        s = db.scalar(select(MeetFundSettings))
        if s is None:
            s = MeetFundSettings()
            db.add(s)
        s.start_month = START
        s.start_balance_chf = Decimal("0")
        s.monthly_a_chf = Decimal("300")
        s.holiday_account_id = konto.id
        db.flush()
        try:
            yield db, s, konto
        finally:
            db.rollback()


def _ruecklage(db, monat: date, betrag: str, person: str = "a") -> None:
    db.add(MeetContribution(month=monat, person=person, amount_native=Decimal(betrag)))
    db.flush()


def _buchung(db, konto: Account, tag: date, betrag: str, text: str,
             *, umbuchung: bool = False) -> Transaction:
    tx = Transaction(
        account_id=konto.id, date=tag, amount=Decimal(betrag), description=text,
        management_type=ManagementType.TRANSFER if umbuchung else None,
    )
    db.add(tx)
    db.flush()
    return tx


# ---------------------------------------------------------------------------
# Abgleich: liegt das Geld da?
# ---------------------------------------------------------------------------


def test_ohne_konto_gibt_es_keinen_abgleich(fonds):
    """Kein Ferienkonto gewählt → der ganze Abschnitt entfällt.

    Nicht „ein leerer Kasten mit Hinweis": ein Kasten, der nur meldet, dass er
    nichts zu melden hat, ist genau der Fülltext, den diese Oberfläche nicht führt.
    """
    db, s, _ = fonds
    s.holiday_account_id = None
    db.flush()
    assert meet_fund.rueckstellung(db, s, HEUTE) is None
    assert meet_fund.verbrauch(db, s, HEUTE) == []


def test_bestaetigt_aber_nicht_ueberwiesen(fonds):
    """Der Fall, um den es geht: drei Monate bestätigt, nichts überwiesen."""
    db, s, _ = fonds
    for monat in (date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1)):
        _ruecklage(db, monat, "300")

    r = meet_fund.rueckstellung(db, s, HEUTE)
    assert r["soll"] == Decimal("900.00")
    assert r["ist"] == Decimal("0.00")
    assert r["offen"] == Decimal("900.00")
    assert r["geht_auf"] is False


def test_umbuchung_zaehlt_als_zufluss(fonds):
    """Der Kern der Rechnung — und die Stelle, an der sie leicht falsch wird.

    Überall sonst filtert die App Umbuchungen weg (``not_transfer``). Genau hier
    darf sie es nicht: die Rückstellung IST eine Umbuchung vom Lohnkonto aufs
    Ferienkonto. Mit dem üblichen Filter meldete der Abgleich beharrlich, es sei
    nichts überwiesen worden.
    """
    db, s, konto = fonds
    _ruecklage(db, date(2026, 5, 1), "300")
    _buchung(db, konto, date(2026, 5, 25), "300", "Rückstellung Ferien", umbuchung=True)

    r = meet_fund.rueckstellung(db, s, HEUTE)
    assert r["ist"] == Decimal("300.00")
    assert r["geht_auf"] is True
    assert r["offen"] == Decimal("0")


def test_zu_viel_ueberwiesen_wird_benannt(fonds):
    """Mehr auf dem Konto als bestätigt — auch das ist eine Abweichung."""
    db, s, konto = fonds
    _ruecklage(db, date(2026, 5, 1), "300")
    _buchung(db, konto, date(2026, 5, 25), "500", "Rückstellung", umbuchung=True)

    r = meet_fund.rueckstellung(db, s, HEUTE)
    assert r["zuviel"] == Decimal("200.00")
    assert r["geht_auf"] is False


def test_euro_von_b_zaehlen_nicht_mit(fonds):
    """Das Geld von B liegt bei B, nicht auf dem eigenen Konto.

    Zählte es beim Soll mit, klaffte auf dem Ferienkonto dauerhaft eine Lücke,
    die nie zugehen kann.
    """
    db, s, konto = fonds
    _ruecklage(db, date(2026, 5, 1), "300")
    _ruecklage(db, date(2026, 5, 1), "100", person="b")
    _buchung(db, konto, date(2026, 5, 25), "300", "Rückstellung", umbuchung=True)

    assert meet_fund.rueckstellung(db, s, HEUTE)["geht_auf"] is True


def test_geloeschtes_konto_erfindet_keine_luecke(fonds):
    """Die Spalte hält nur eine Nummer.

    Zeigt sie ins Leere, wären die Zuflüsse null und der Abgleich meldete eine
    Lücke in voller Höhe der Rücklagen — ein Fehlalarm aus einem Datenrest.
    """
    db, s, konto = fonds
    _ruecklage(db, date(2026, 5, 1), "300")
    s.holiday_account_id = konto.id + 9999
    db.flush()

    assert meet_fund.rueckstellung(db, s, HEUTE) is None


def test_abfluss_zaehlt_nicht_als_zufluss(fonds):
    """Bezahlte Ferien mindern das Konto — bestätigt zurückgelegt bleibt es trotzdem."""
    db, s, konto = fonds
    _ruecklage(db, date(2026, 5, 1), "300")
    _buchung(db, konto, date(2026, 5, 25), "300", "Rückstellung", umbuchung=True)
    _buchung(db, konto, date(2026, 6, 2), "-120", "Flug")

    r = meet_fund.rueckstellung(db, s, HEUTE)
    assert r["ist"] == Decimal("300.00")


# ---------------------------------------------------------------------------
# Verbrauch: was hat die Reise gekostet?
# ---------------------------------------------------------------------------


def test_laufende_reise_wird_noch_nicht_abgerechnet(fonds):
    """„Wenn Ferien durch" heisst: der letzte Reisetag ist vorbei, nicht der erste."""
    db, s, _ = fonds
    db.add(MeetVisit(date=HEUTE - timedelta(days=1), location="bei_b", nights=3))
    db.flush()

    assert meet_fund.verbrauch(db, s, HEUTE) == []


def test_abgeschlossene_reise_misst_die_abfluesse(fonds):
    """Gerechnete Kosten gegen das, was das Konto wirklich verlassen hat."""
    db, s, konto = fonds
    v = MeetVisit(date=date(2026, 5, 8), location="bei_b", nights=3,
                  cost_override_chf=Decimal("900"))
    db.add(v)
    db.flush()
    _buchung(db, konto, date(2026, 4, 20), "-400", "Flug")       # im Voraus
    _buchung(db, konto, date(2026, 5, 9), "-250", "Unterkunft")  # während
    _buchung(db, konto, date(2026, 5, 20), "-100", "Kartenzahlung")  # Nachlauf

    zeilen = meet_fund.verbrauch(db, s, HEUTE)
    assert len(zeilen) == 1
    assert zeilen[0]["gerechnet"] == Decimal("900.00")
    assert zeilen[0]["abgang"] == Decimal("750.00")
    assert zeilen[0]["differenz"] == Decimal("-150.00")


def test_zwei_reisen_teilen_sich_die_abfluesse_nicht(fonds):
    """Die Fenster überschneiden sich nicht — sonst zählte ein Betrag doppelt.

    Ein Beleg zwischen zwei Reisen gehört zur späteren: er ist die Vorauszahlung
    für die nächste, nicht die Nachzahlung der letzten.
    """
    db, s, konto = fonds
    db.add(MeetVisit(date=date(2026, 3, 6), location="bei_b", nights=3,
                     cost_override_chf=Decimal("800")))
    db.add(MeetVisit(date=date(2026, 6, 5), location="bei_b", nights=3,
                     cost_override_chf=Decimal("800")))
    db.flush()
    _buchung(db, konto, date(2026, 3, 7), "-500", "Reise eins")
    _buchung(db, konto, date(2026, 5, 2), "-600", "Flug fuer Reise zwei")

    zeilen = meet_fund.verbrauch(db, s, HEUTE)
    assert [z["abgang"] for z in zeilen] == [Decimal("500.00"), Decimal("600.00")]


def test_von_anderem_konto_bezahlt_wird_benannt(fonds):
    """Null Abfluss ist keine Fehlmeldung, sondern eine Antwort.

    Die Rückstellung liegt dann noch da — sie wurde für diese Reise nicht
    gebraucht.
    """
    db, s, _ = fonds
    db.add(MeetVisit(date=date(2026, 5, 8), location="bei_b", nights=3,
                     cost_override_chf=Decimal("900")))
    db.flush()

    zeilen = meet_fund.verbrauch(db, s, HEUTE)
    assert zeilen[0]["nichts_bezahlt"] is True
    assert zeilen[0]["abgang"] == Decimal("0.00")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_konto_waehlen_und_wieder_loesen(logged_in_client):
    """Leer ist eine Wahl, nicht „unverändert" — sonst liesse sich die
    Verknüpfung nie wieder lösen."""
    with SessionLocal() as db:
        konto = Account(name="Ferienkonto (Route)", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"))
        db.add(konto)
        db.commit()
        konto_id = konto.id

    r = logged_in_client.post("/savings-goals/meet/settings",
                              data={"holiday_account_id": str(konto_id)})
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(MeetFundSettings)).holiday_account_id == konto_id

    r = logged_in_client.post("/savings-goals/meet/settings", data={"holiday_account_id": ""})
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(MeetFundSettings)).holiday_account_id is None


def test_unbekanntes_konto_wird_abgelehnt(logged_in_client):
    """Eine Nummer, die es nicht gibt, darf die bestehende Wahl nicht ersetzen.

    Der Ausgangszustand wird hier gesetzt und nicht vom vorigen Test geerbt: eine
    Prüfung, die von der Reihenfolge der Tests abhängt, misst irgendwann etwas
    anderes als sie behauptet.
    """
    with SessionLocal() as db:
        konto = Account(name="Ferienkonto (Bestand)", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"))
        db.add(konto)
        db.flush()
        s = db.scalar(select(MeetFundSettings)) or MeetFundSettings()
        s.holiday_account_id = konto.id
        db.add(s)
        db.commit()
        vorher = konto.id

    r = logged_in_client.post("/savings-goals/meet/settings",
                              data={"holiday_account_id": "999999"})
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(MeetFundSettings)).holiday_account_id == vorher
