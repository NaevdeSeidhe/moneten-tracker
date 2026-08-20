"""Tests für Prognose + Stresstest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from moneten.db.session import SessionLocal
from moneten.services.forecasting import monthly_in_out, net_worth_projection, stresstest


def test_stresstest_pure_runway() -> None:
    # Einkommen 5000, Ausgaben 4000 → −15% Einkommen, +10% Ausgaben.
    r = stresstest(
        base_income=Decimal("5000"), base_expense=Decimal("4000"),
        income_pct=-15, expense_pct=10, one_time=Decimal("0"), liquid=Decimal("10000"),
    )
    assert r.new_income == Decimal("4250.00")
    assert r.new_expense == Decimal("4400.00")
    assert r.new_saldo == Decimal("-150.00")
    assert r.base_saldo == Decimal("1000.00")
    # Runway = 10'000 / 150 ≈ 66.7 Monate
    assert r.runway_months is not None and 66 <= r.runway_months <= 67


def test_stresstest_positive_saldo_unlimited() -> None:
    r = stresstest(
        base_income=Decimal("5000"), base_expense=Decimal("4000"),
        income_pct=0, expense_pct=0, one_time=Decimal("0"), liquid=Decimal("8000"),
    )
    assert r.new_saldo == Decimal("1000.00")
    assert r.runway_months is None  # positiver Saldo → unbegrenzt


def test_projection_and_baseline_run() -> None:
    with SessionLocal() as db:
        inc, exp = monthly_in_out(db, date.today())
        assert inc >= 0 and exp >= 0
        proj = net_worth_projection(db, date.today())
        assert proj.width > 0 and proj.height > 0
        assert isinstance(proj.end_value, Decimal)
        # Alle Stützpunkte (Historie + Prognose) liegen vor — der Client braucht
        # deren Rohwerte, um die Stresstest-Y-Skala mitwachsen zu lassen.
        assert len(proj.points) == proj.hist_len + proj.horizon
        assert all("value" in p for p in proj.points)


def test_forecast_chart_cfg_exposes_raw_values(logged_in_client: TestClient) -> None:
    # Der Stresstest zeichnet die Linie client-seitig und skaliert die Y-Achse mit.
    # Dazu MUSS das Chart-Konfig-Objekt die Rohwerte aller Stützpunkte enthalten,
    # sonst würde die Szenario-Linie am Diagrammrand abgeschnitten (sichtbarer „Knick").
    page = logged_in_client.get("/forecast")
    assert page.status_code == 200
    assert '"vals"' in page.text


def test_forecast_page_and_stresstest_route(logged_in_client: TestClient) -> None:
    page = logged_in_client.get("/forecast")
    assert page.status_code == 200
    assert "Stresstest" in page.text
    assert "12-Monats-Prognose" in page.text

    resp = logged_in_client.post("/forecast/stresstest",
                                 data={"income_pct": "-20", "expense_pct": "15", "one_time": "500"})
    assert resp.status_code == 200
    assert "Monatssaldo im Szenario" in resp.text


def test_runway_null_wenn_einmalausgabe_die_reserve_uebersteigt() -> None:
    """Positiver Monatssaldo darf eine untragbare Einmalausgabe nicht kaschieren.

    Regressionstest: geprüft wurde nur das Vorzeichen des Saldos, `one_time` fiel
    dabei unter den Tisch — 5'000 Reserve und 20'000 Sonderausgabe meldeten
    „unbegrenzt".
    """
    from decimal import Decimal

    from moneten.services.forecasting import stresstest

    r = stresstest(
        base_income=Decimal("5000"), base_expense=Decimal("4000"),
        income_pct=0, expense_pct=0,
        one_time=Decimal("20000"), liquid=Decimal("5000"),
    )
    assert r.new_saldo > 0, "Monatssaldo ist positiv — genau der heikle Fall"
    assert r.runway_months == 0.0, "Reserve ist längst aufgebraucht, nicht unbegrenzt"


def test_runway_unbegrenzt_ohne_einmalausgabe() -> None:
    """Gegenprobe: ohne Sonderausgabe bleibt der positive Saldo unbegrenzt."""
    from decimal import Decimal

    from moneten.services.forecasting import stresstest

    r = stresstest(
        base_income=Decimal("5000"), base_expense=Decimal("4000"),
        income_pct=0, expense_pct=0,
        one_time=Decimal("0"), liquid=Decimal("5000"),
    )
    assert r.runway_months is None
