"""Geht der Kontoauszug in sich auf?

Die Prüfung braucht keine Datenbank — sie rechnet nur innerhalb einer Datei.
Alle Zahlen hier sind erfundene Testwerte.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from moneten.services.camt053_parser import Camt053Entry, Camt053Statement
from moneten.services.statement_check import pruefe_auszug


def _entry(betrag: str, tag: int = 5) -> Camt053Entry:
    return Camt053Entry(
        date=date(2016, 3, tag),
        value_date=date(2016, 3, tag),
        amount=Decimal(betrag),
        description="ZZZ Testbuchung",
        reference=None,
    )


def _auszug(anfang, schluss, betraege: list[str]) -> Camt053Statement:
    return Camt053Statement(
        iban="CH00 0000 0000 0000 0000 0",
        currency="CHF",
        period_from=date(2016, 3, 1),
        period_to=date(2016, 3, 31),
        opening_balance=Decimal(anfang) if anfang is not None else None,
        closing_balance=Decimal(schluss) if schluss is not None else None,
        entries=[_entry(b, tag=i + 1) for i, b in enumerate(betraege)],
    )


def test_stimmiger_auszug_geht_auf() -> None:
    """1000 + (-200) + (-50) + 300 = 1050."""
    p = pruefe_auszug(_auszug("1000", "1050", ["-200", "-50", "300"]))
    assert p.pruefbar and p.stimmt
    assert p.erwartet == Decimal("1050")
    assert p.differenz == Decimal("0")
    assert p.anzahl == 3


def test_fehlende_buchung_faellt_auf() -> None:
    """Der eigentliche Zweck: die Bank nennt einen Saldo, den die Buchungen nicht ergeben.

    Hier fehlt eine Belastung von 120 — die Datei war unvollständig oder der
    Parser hat eine Zeile verloren. Ohne diese Prüfung importiert die App still
    zu wenig und die Differenz taucht erst Monate später auf.
    """
    p = pruefe_auszug(_auszug("1000", "930", ["-200", "-50", "300"]))
    assert p.pruefbar and p.stimmt is False
    assert p.differenz == Decimal("-120")
    assert "Differenz" in p.hinweis
    assert "unvollständig" in p.hinweis


def test_ein_rappen_gilt_als_rundung() -> None:
    p = pruefe_auszug(_auszug("1000", "1050.01", ["-200", "-50", "300"]))
    assert p.stimmt, "Ein Rappen ist Darstellung, keine fehlende Buchung"


def test_zwei_rappen_nicht_mehr() -> None:
    p = pruefe_auszug(_auszug("1000", "1050.02", ["-200", "-50", "300"]))
    assert p.stimmt is False, "Die Toleranz darf nicht schleichend wachsen"


def test_ohne_salden_kein_urteil() -> None:
    """Manche Institute liefern nur einen der beiden Salden.

    Dann muss dort „nicht prüfbar" stehen — nicht „geprüft und in Ordnung".
    Ein stiller Haken auf einer Prüfung, die gar nicht lief, ist schlimmer als
    gar keine Prüfung.
    """
    for anfang, schluss in ((None, "1050"), ("1000", None), (None, None)):
        p = pruefe_auszug(_auszug(anfang, schluss, ["-200"]))
        assert p.pruefbar is False
        assert p.stimmt is None
        assert "lässt sich nicht" in p.hinweis


def test_leerer_auszug_mit_gleichen_salden_geht_auf() -> None:
    p = pruefe_auszug(_auszug("1000", "1000", []))
    assert p.stimmt and p.anzahl == 0
