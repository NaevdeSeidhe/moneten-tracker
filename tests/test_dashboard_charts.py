"""Tests für die Dashboard-Diagramm-Helfer (Donut-Gruppen, Sparkline, Trend).

Bewusst reine Funktionen ohne DB — deterministisch und unabhängig von der
Test-Reihenfolge (die session-weite Test-DB wird von anderen Tests befüllt).
"""

from __future__ import annotations

from decimal import Decimal

from moneten.db.models import Account, AccountType
from moneten.routers.dashboard import (
    _LIQUID_TYPES,
    _SAVINGS_TYPES,
    _account_chart,
    _pct_change,
    _sparkline,
)
from moneten.services.sankey import build_flow


def _acc(name: str, typ: AccountType, balance: str, active: bool = True, order: int = 0) -> Account:
    """In-Memory-Konto (nicht persistiert) für die Chart-Berechnung."""
    return Account(
        name=name, type=typ, currency="CHF", is_active=active,
        opening_balance=Decimal(balance), current_balance=Decimal(balance), sort_order=order,
    )


# ----------  Vormonatsvergleich  ----------
def test_pct_change_basic() -> None:
    assert _pct_change(Decimal("110"), Decimal("100")) == 10.0
    assert _pct_change(Decimal("90"), Decimal("100")) == -10.0


def test_pct_change_negative_base_uses_abs() -> None:
    # Saldo von -100 auf -50 = Verbesserung um +50 % (Betrag der Basis).
    assert _pct_change(Decimal("-50"), Decimal("-100")) == 50.0


def test_pct_change_guards() -> None:
    assert _pct_change(Decimal("50"), Decimal("0")) is None
    assert _pct_change(Decimal("50"), None) is None


# ----------  Sparkline-Geometrie  ----------
def test_sparkline_empty() -> None:
    sp = _sparkline([])
    assert sp["flat"] is True
    assert sp["line"] == ""
    assert sp["pts"] == []


def test_sparkline_flat_line() -> None:
    sp = _sparkline([Decimal("5"), Decimal("5"), Decimal("5")], h=34)
    assert sp["flat"] is True
    assert len(sp["pts"]) == 3
    # Alle Punkte auf der Mittellinie (h/2 = 17).
    assert {y for _, y in sp["pts"]} == {17.0}
    # Geglätteter Pfad beginnt mit einem Move-Befehl.
    assert sp["line"].startswith("M ")


def test_sparkline_ascending_last_point_high() -> None:
    sp = _sparkline([Decimal("0"), Decimal("5"), Decimal("10")], h=34, pad=4)
    assert sp["flat"] is False
    # Höchster Wert zuletzt → kleinster y-Wert (oben), klar über der Mitte.
    assert sp["last_y"] < 17
    # Weiche Kurve nutzt kubische Béziers; Fläche schliesst unten ab.
    assert " C " in sp["line"]
    assert sp["area"].strip().endswith("Z")


# ----------  Konto-Gruppen-Donut  ----------
def test_account_chart_liquid_group() -> None:
    accs = [
        _acc("Bank", AccountType.BANK, "100", order=1),
        _acc("Cash", AccountType.CASH, "50", order=2),
        _acc("Spar", AccountType.SAVINGS, "900", order=3),
    ]
    liquid = _account_chart(accs, _LIQUID_TYPES)
    assert liquid["total"] == Decimal("150")
    assert len(liquid["segments"]) == 2
    # Anteile: 100/150 = 66.7 %, 50/150 = 33.3 %
    assert liquid["segments"][0]["pct"] == 66.7
    assert liquid["segments"][1]["pct"] == 33.3


def test_account_chart_zero_accounts_visible_in_legend() -> None:
    accs = [
        _acc("Sparkonto", AccountType.SAVINGS, "900", order=1),
        _acc("Crypto", AccountType.CRYPTO, "0", active=False, order=2),
        _acc("Aktien", AccountType.STOCKS, "0", active=False, order=3),
    ]
    savings = _account_chart(accs, _SAVINGS_TYPES)
    # Nur das Sparkonto bildet ein Segment …
    assert len(savings["segments"]) == 1
    # … aber Crypto & Aktien bleiben in der Legende sichtbar (als inaktiv).
    names = [item["name"] for item in savings["legend"]]
    assert names == ["Sparkonto", "Crypto", "Aktien"]
    inaktiv = {item["name"]: item["active"] for item in savings["legend"]}
    assert inaktiv["Crypto"] is False
    assert inaktiv["Aktien"] is False


# ----------  Sankey-Geldfluss  ----------
def test_sankey_balances_with_surplus() -> None:
    flow = build_flow(
        [("Lohn", Decimal("7000"))],
        [("Wohnen", Decimal("1800")), ("Konsum", Decimal("500"))],
    )
    assert flow is not None
    # Rechts: 2 Ausgaben + „Überschuss · Sparen".
    labels = [n["label"] for n in flow["right"]]
    assert "Überschuss · Sparen" in labels
    assert len(flow["right"]) == 3
    # Hub-Höhe entspricht der Gesamtsumme (= Einnahmen, da Überschuss > 0).
    assert flow["total"] == Decimal("7000")
    # Bänder: 1 (Einnahme→Hub) + 3 (Hub→Ausgaben).
    assert len(flow["links"]) == 4


def test_sankey_deficit_adds_reserve() -> None:
    flow = build_flow([("Lohn", Decimal("1000"))], [("Wohnen", Decimal("1500"))])
    assert flow is not None
    left_labels = [n["label"] for n in flow["left"]]
    assert "aus Reserve" in left_labels
    assert flow["total"] == Decimal("1500")


def test_sankey_empty_returns_none() -> None:
    assert build_flow([], []) is None
