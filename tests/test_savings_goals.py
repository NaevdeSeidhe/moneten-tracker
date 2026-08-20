"""Tests für die Sparziele-Seite (CRUD + Fortschritt aus verknüpftem Konto)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.dates import add_months
from moneten.db.models import Account, AccountType, SavingsGoal, Transaction
from moneten.db.session import SessionLocal
from moneten.services.balances import recalc_account_balance


def test_savings_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/savings-goals")
    assert resp.status_code == 200
    assert "Sparziele" in resp.text


def test_create_goal_with_progress(logged_in_client: TestClient) -> None:
    # Konto mit Saldo 2'500 → Fortschritt 25 % bei Ziel 10'000.
    with SessionLocal() as db:
        acc = Account(name="Sparkonto-Test", type=AccountType.SAVINGS, currency="CHF",
                      opening_balance=Decimal("2500"), current_balance=Decimal("2500"), sort_order=920)
        db.add(acc)
        db.commit()
        acc_id = acc.id

    resp = logged_in_client.post("/savings-goals", data={
        "name": "ZZZ-Reise", "target_amount": "10000",
        "account_id": str(acc_id), "priority": "high",
    })
    assert resp.status_code == 200
    assert "ZZZ-Reise" in resp.text
    assert "25%" in resp.text  # Fortschrittsanzeige

    with SessionLocal() as db:
        g = db.scalar(select(SavingsGoal).where(SavingsGoal.name == "ZZZ-Reise"))
        assert g is not None and g.target_amount == Decimal("10000")
        gid = g.id

    # Erreicht markieren …
    logged_in_client.post(f"/savings-goals/{gid}/toggle")
    with SessionLocal() as db:
        assert db.get(SavingsGoal, gid).is_achieved is True

    # … und löschen.
    logged_in_client.post(f"/savings-goals/{gid}/delete")
    with SessionLocal() as db:
        assert db.get(SavingsGoal, gid) is None


def test_goal_forecast_shows_completion(logged_in_client: TestClient) -> None:
    """Konto mit stetig wachsendem Saldo → Prognose-Datum erscheint."""
    from datetime import date
    today = date.today()
    with SessionLocal() as db:
        acc = Account(name="ZZZ-Prognose-Konto", type=AccountType.SAVINGS, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=940)
        db.add(acc)
        db.flush()
        # 6 monatliche Einzahlungen à 200 → wachsender Saldo (Sparrate ≈ 200/Mt).
        for k in range(6):
            m = add_months(today, -k).replace(day=15)
            db.add(Transaction(account_id=acc.id, date=m, amount=Decimal("200.00"),
                               description="Sparen"))
        db.flush()
        recalc_account_balance(db, acc.id)
        db.commit()
        acc_id = acc.id

    logged_in_client.post("/savings-goals", data={
        "name": "ZZZ-Prognoseziel", "target_amount": "2000", "account_id": str(acc_id), "priority": "medium",
    })
    resp = logged_in_client.get("/savings-goals")
    assert resp.status_code == 200
    assert "Voraussichtlich erreicht" in resp.text


def test_create_goal_validation(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/savings-goals", data={"name": "", "target_amount": "100"})
    assert resp.status_code == 400
    assert "Namen" in resp.text


# ---------------------------------------------------------------------------
# Verteilung des Kontosaldos auf mehrere Ziele am selben Konto
# ---------------------------------------------------------------------------


def test_allocate_savings_teilt_saldo_statt_ihn_zu_vervielfachen() -> None:
    """Zwei Ziele an EINEM Konto dürfen den Saldo nicht doppelt ausweisen.

    Regressionstest zum gefundenen Rechenfehler: vorher bekam jedes Ziel den
    vollen Kontosaldo, die Seitensumme zeigte darum „angespart 25'000" bei
    einem Gesamtziel von 14'000.
    """
    from moneten.db.models import GoalPriority
    from moneten.routers.savings_goals import allocate_savings

    notgroschen = SavingsGoal(id=1, name="Notgroschen", target_amount=Decimal("10000"),
                              account_id=7, priority=GoalPriority.HIGH, is_achieved=False)
    ferien = SavingsGoal(id=2, name="Ferien", target_amount=Decimal("4000"),
                         account_id=7, priority=GoalPriority.LOW, is_achieved=False)

    z = allocate_savings([notgroschen, ferien], {7: Decimal("12500")})

    assert z[1] == Decimal("10000"), "höhere Priorität wird zuerst bedient"
    assert z[2] == Decimal("2500"), "nur der Rest fliesst ins zweite Ziel"
    assert sum(z.values()) == Decimal("12500"), "die Summe bleibt der echte Saldo"


def test_allocate_savings_deckelt_auf_den_zielbetrag() -> None:
    """Ein überfinanziertes Konto macht aus 10'000 Ziel keine 12'500 „angespart"."""
    from moneten.db.models import GoalPriority
    from moneten.routers.savings_goals import allocate_savings

    g = SavingsGoal(id=1, name="Notgroschen", target_amount=Decimal("10000"),
                    account_id=7, priority=GoalPriority.MEDIUM, is_achieved=False)
    assert allocate_savings([g], {7: Decimal("12500")})[1] == Decimal("10000")


def test_allocate_savings_erledigte_ziele_zuerst() -> None:
    """Geld eines erreichten Ziels ist gebunden und fehlt den offenen Zielen."""
    from moneten.db.models import GoalPriority
    from moneten.routers.savings_goals import allocate_savings

    fertig = SavingsGoal(id=1, name="Fertig", target_amount=Decimal("3000"),
                         account_id=7, priority=GoalPriority.LOW, is_achieved=True)
    offen = SavingsGoal(id=2, name="Offen", target_amount=Decimal("5000"),
                        account_id=7, priority=GoalPriority.HIGH, is_achieved=False)

    z = allocate_savings([fertig, offen], {7: Decimal("4000")})
    assert z[1] == Decimal("3000")
    assert z[2] == Decimal("1000")


def test_allocate_savings_negativer_saldo_wird_null() -> None:
    from moneten.db.models import GoalPriority
    from moneten.routers.savings_goals import allocate_savings

    g = SavingsGoal(id=1, name="Ziel", target_amount=Decimal("500"),
                    account_id=7, priority=GoalPriority.MEDIUM, is_achieved=False)
    assert allocate_savings([g], {7: Decimal("-200")})[1] == Decimal("0")


def test_savings_summe_zaehlt_geteiltes_konto_nur_einmal(logged_in_client: TestClient) -> None:
    """Ende-zu-Ende über die Seite: zwei Ziele, ein Konto, Summe = Saldo."""
    with SessionLocal() as db:
        acc = Account(name="ZZZ-Geteiltes-Sparkonto", type=AccountType.SAVINGS, currency="CHF",
                      opening_balance=Decimal("12500"), current_balance=Decimal("12500"),
                      sort_order=950)
        db.add(acc)
        db.commit()
        acc_id = acc.id

    for name, betrag, prio in [("ZZZ-Notgroschen", "10000", "high"), ("ZZZ-Ferien", "4000", "low")]:
        logged_in_client.post("/savings-goals", data={
            "name": name, "target_amount": betrag, "account_id": str(acc_id), "priority": prio,
        })

    resp = logged_in_client.get("/savings-goals")
    assert resp.status_code == 200
    # Der volle Saldo darf höchstens EINMAL als angesparter Betrag auftauchen —
    # vorher stand er einmal je Ziel plus in der Summe.
    assert resp.text.count("CHF 12'500.00") <= 1
