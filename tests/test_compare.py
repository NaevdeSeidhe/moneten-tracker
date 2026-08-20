"""Tests für die Vergleichsansicht (Monat/Jahr)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import delete

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal
from moneten.services.comparison import _stichtag_grenze, build_comparison
from moneten.templating import MONATE


@contextmanager
def _erfundene_buchungen(posten: list[tuple[date, str]]) -> Iterator[None]:
    """Legt frei erfundene Buchungen auf einem eigenen Konto an — und räumt sie weg.

    Die Test-DB lebt über den ganzen Lauf. Bliebe hier etwas liegen, verschöbe es
    die Summen anderer Module; umgekehrt liegen die verwendeten Jahre (2011/2012)
    weit vor allem, was andere Tests anlegen, damit die erwarteten Beträge exakt
    stimmen statt nur „ungefähr".
    """
    with SessionLocal() as db:
        konto = Account(
            name="Vergleich-Stichtag-Konto", type=AccountType.BANK, currency="CHF",
            opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=900,
        )
        db.add(konto)
        db.flush()
        konto_id = konto.id
        for tag, betrag in posten:
            db.add(Transaction(account_id=konto_id, date=tag, amount=Decimal(betrag),
                               description="Erfunden"))
        db.commit()
    try:
        yield
    finally:
        with SessionLocal() as db:
            db.execute(delete(Transaction).where(Transaction.account_id == konto_id))
            db.execute(delete(Account).where(Account.id == konto_id))
            db.commit()


def test_build_comparison_structure() -> None:
    with SessionLocal() as db:
        c = build_comparison(db, date.today())
    assert {"month_cur", "month_prev", "month_cats", "year_cur", "year_prev",
            "stichtag_label"} <= set(c)
    # Totals sind plausible Decimals.
    assert isinstance(c["month_cur"].income, Decimal)
    assert c["month_cur"].saldo == c["month_cur"].income - c["month_cur"].expense


def test_stichtag_grenze_kennt_kurze_monate() -> None:
    """Der 29. Februar im Nicht-Schaltjahr darf keine Ausnahme werfen."""
    assert _stichtag_grenze(2011, 2, 29) == date(2011, 3, 1)   # 2011 hat keinen 29.2.
    assert _stichtag_grenze(2012, 2, 29) == date(2012, 3, 1)   # Schaltjahr, letzter Tag
    assert _stichtag_grenze(2011, 4, 31) == date(2011, 5, 1)   # April hat 30 Tage
    assert _stichtag_grenze(2011, 5, 10) == date(2011, 5, 11)  # Normalfall: Folgetag


def test_jahresvergleich_schneidet_vorjahr_am_stichtag_ab() -> None:
    """Vorjahr bis zum selben Kalendertag — sonst steht ein Teiljahr gegen ein Ganzes."""
    heute = date(2012, 5, 10)
    with _erfundene_buchungen([
        (date(2011, 3, 5), "5000.00"), (date(2011, 3, 6), "-1200.00"),    # vor dem Stichtag
        (date(2011, 9, 20), "9000.00"), (date(2011, 9, 21), "-4000.00"),  # danach: zählt NICHT
        (date(2012, 3, 5), "5000.00"), (date(2012, 3, 6), "-1200.00"),
    ]), SessionLocal() as db:
        c = build_comparison(db, heute)
    assert c["year_prev"].income == Decimal("5000.00")
    assert c["year_prev"].expense == Decimal("1200.00")
    # Gleiche Buchungen im gleich langen Zeitraum → kein Unterschied.
    assert c["year_cur"].saldo == c["year_prev"].saldo
    assert c["stichtag_label"] == "10. Mai"


def test_jahresvergleich_am_29_februar() -> None:
    """Stichtag 29.2.: im Vorjahr gibt es den Tag nicht — der Zeitraum endet mit dem Monat."""
    heute = date(2012, 2, 29)  # Schaltjahr; 2011 hat nur 28 Februartage
    with _erfundene_buchungen([
        (date(2011, 2, 28), "700.00"),   # letzter Tag des Vorjahres-Februars: zählt
        (date(2011, 3, 1), "9000.00"),   # einen Tag danach: zählt NICHT
        (date(2012, 2, 29), "700.00"),
    ]), SessionLocal() as db:
        c = build_comparison(db, heute)
    assert c["year_prev"].income == Decimal("700.00")
    assert c["year_cur"].income == Decimal("700.00")


def test_monatsvergleich_schneidet_vormonat_am_stichtag_ab() -> None:
    """Dieselbe Ursache im Monatspaar: angefangener Monat gegen vollen Vormonat."""
    heute = date(2012, 5, 10)
    with _erfundene_buchungen([
        (date(2012, 4, 3), "-300.00"),    # Vormonat vor dem Stichtag
        (date(2012, 4, 25), "-800.00"),   # Vormonat danach: zählt NICHT
        (date(2012, 5, 3), "-300.00"),
        (date(2012, 5, 28), "-800.00"),   # laufender Monat, künftig datiert: zählt NICHT
    ]), SessionLocal() as db:
        c = build_comparison(db, heute)
    assert c["month_prev"].expense == Decimal("300.00")
    assert c["month_cur"].expense == Decimal("300.00")
    # Auch die Kategorie-Balken müssen auf den Stichtag laufen, sonst widerspricht
    # die Liste den Kacheln darüber.
    ohne = next(z for z in c["month_cats"] if z["name"] == "Ohne Kategorie")
    assert ohne["cur"] == Decimal("300.00")
    assert ohne["prev"] == Decimal("300.00")
    assert ohne["delta"] == Decimal("0")


def test_compare_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/compare")
    assert resp.status_code == 200
    assert "Vergleich" in resp.text
    assert "vs. Vormonat" in resp.text


def test_compare_page_nennt_gemeinsamen_stichtag(logged_in_client: TestClient) -> None:
    """Ohne diese Angabe liest man den gekürzten Vorjahreswert als Einbruch."""
    resp = logged_in_client.get("/compare")
    heute = date.today()
    assert f"beide bis zum {heute.day}. {MONATE[heute.month - 1]}" in resp.text
    assert f"beide bis zum {heute.day}." in resp.text
