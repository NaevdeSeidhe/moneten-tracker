"""Tests für den Budget-Editor: Ist-Berechnung, Median, Soll setzen, Ampel."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    Budget,
    BudgetInterval,
    Category,
    StandardBudget,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services.median_budget import (
    ampel_status,
    autofill_standard_budgets,
    ist_for_category,
    monthly_equivalent,
)


def _account_id() -> int:
    with SessionLocal() as db:
        acc = Account(name="Budget-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=600)
        db.add(acc)
        db.commit()
        return acc.id


def _category_id(name: str = "Kaffee") -> int:
    with SessionLocal() as db:
        return db.scalar(select(Category).where(Category.name == name)).id


def test_ist_for_category_nets_refunds() -> None:
    acc = _account_id()
    cat = _category_id("Kaffee")
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        db.add(Transaction(account_id=acc, category_id=cat, date=m, amount=Decimal("-5.00"), description="Kaffee 1"))
        db.add(Transaction(account_id=acc, category_id=cat, date=m, amount=Decimal("-7.50"), description="Kaffee 2"))
        # Gutschrift/Storno in derselben Kategorie wird gegengerechnet (reduziert die Ausgabe).
        db.add(Transaction(account_id=acc, category_id=cat, date=m, amount=Decimal("3.00"), description="Storno"))
        db.commit()
        ist = ist_for_category(db, cat, m)
    assert ist == Decimal("9.50")  # 12.50 Ausgabe − 3.00 Gutschrift


def test_ist_refund_exceeding_clamps_to_zero() -> None:
    """Übersteigt die Gutschrift die Ausgabe, ist die Ausgabe 0 (nie negativ)."""
    acc = _account_id()
    cat = _category_id("Alkohol")
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        db.add(Transaction(account_id=acc, category_id=cat, date=m, amount=Decimal("-20.00"), description="Kauf"))
        db.add(Transaction(account_id=acc, category_id=cat, date=m, amount=Decimal("50.00"), description="Grosse Gutschrift"))
        db.commit()
        ist = ist_for_category(db, cat, m)
    assert ist == Decimal("0")


def test_ampel_status() -> None:
    assert ampel_status(None, Decimal("50")) == "none"
    assert ampel_status(Decimal("100"), Decimal("50")) == "ok"     # 50%
    assert ampel_status(Decimal("100"), Decimal("90")) == "warn"   # 90%
    assert ampel_status(Decimal("100"), Decimal("120")) == "over"  # 120%


def test_budget_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/budget")
    assert resp.status_code == 200
    assert "Budget" in resp.text
    # Ausgaben-Kategorien erscheinen, Einnahmen nicht.
    assert "Konsum" in resp.text
    assert "Kaffee" in resp.text


def test_set_budget(logged_in_client: TestClient) -> None:
    cat = _category_id("Rauchen")
    m = f"{date.today():%Y-%m}"
    resp = logged_in_client.post("/budget/set", data={"category_id": str(cat), "month": m, "soll": "120"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        b = db.scalar(select(Budget).where(Budget.category_id == cat))
        assert b is not None
        assert b.planned_amount == Decimal("120.00")

    # Soll auf 0 setzen entfernt den Eintrag.
    logged_in_client.post("/budget/set", data={"category_id": str(cat), "month": m, "soll": "0"})
    with SessionLocal() as db:
        assert db.scalar(select(Budget).where(Budget.category_id == cat)) is None


def test_monthly_equivalent_yearly_is_twelfth() -> None:
    # Jährlich → 1/12 ins Monatsbudget.
    assert monthly_equivalent(Decimal("1200"), BudgetInterval.JAEHRLICH) == Decimal("100.00")
    # Monatlich → unverändert.
    assert monthly_equivalent(Decimal("90"), BudgetInterval.MONATLICH) == Decimal("90")
    # Nichts gesetzt → 0.
    assert monthly_equivalent(None, BudgetInterval.MONATLICH) == Decimal("0")


def test_set_standard_budget(logged_in_client: TestClient) -> None:
    """Standard-Soll wird gespeichert und bei 0 wieder entfernt."""
    cat = _category_id("Gaming")
    m = f"{date.today():%Y-%m}"
    resp = logged_in_client.post(
        "/budget/standard",
        data={"category_id": str(cat), "month": m, "amount": "90", "interval": "monatlich"},
    )
    assert resp.status_code == 200
    with SessionLocal() as db:
        sb = db.scalar(select(StandardBudget).where(StandardBudget.category_id == cat))
        assert sb is not None
        assert sb.amount == Decimal("90.00")
        assert sb.interval == BudgetInterval.MONATLICH
    # Auf 0 setzen entfernt den Standard.
    logged_in_client.post(
        "/budget/standard",
        data={"category_id": str(cat), "month": m, "amount": "0", "interval": "monatlich"},
    )
    with SessionLocal() as db:
        assert db.scalar(select(StandardBudget).where(StandardBudget.category_id == cat)) is None


def test_autofill_standard_budgets_from_median() -> None:
    """Leere Standard-Soll werden aus dem Median der bisherigen Ausgaben gefüllt;
    vorhandene Werte werden nicht überschrieben. Räumt erzeugte Soll wieder auf."""
    acc = _account_id()
    cat = _category_id("Velo")
    this_month = date.today().replace(day=1)

    def prev(n: int) -> date:
        total = (this_month.year * 12 + this_month.month - 1) - n
        return date(total // 12, total % 12 + 1, 1)

    with SessionLocal() as db:
        before = {sb.category_id for sb in db.scalars(select(StandardBudget))}
        assert cat not in before  # Velo hat noch kein Standard-Soll
        for i, betrag in zip((1, 2, 3), ("100.00", "200.00", "300.00"), strict=True):
            db.add(Transaction(account_id=acc, category_id=cat, date=prev(i),
                               amount=Decimal("-" + betrag), description="Velo-Service"))
        db.commit()

    with SessionLocal() as db:
        created = autofill_standard_budgets(db, this_month)
        assert created >= 1

    with SessionLocal() as db:
        sb = db.scalar(select(StandardBudget).where(StandardBudget.category_id == cat))
        assert sb is not None
        assert sb.amount == Decimal("200")     # Median von 100/200/300, ganze Franken
        assert sb.interval == BudgetInterval.MONATLICH
        # Zweiter Lauf füllt nichts Neues (überschreibt nicht).
        assert autofill_standard_budgets(db, this_month) == 0

    # Aufräumen: alle in diesem Test neu erzeugten Standard-Soll entfernen.
    with SessionLocal() as db:
        new_ids = {x.category_id for x in db.scalars(select(StandardBudget))} - before
        for sb in db.scalars(select(StandardBudget).where(StandardBudget.category_id.in_(new_ids))):
            db.delete(sb)
        db.commit()


def test_yearly_standard_shows_rueckstellung(logged_in_client: TestClient) -> None:
    """Ein jährliches Standard-Soll erscheint im Rückstellungen-Block (mit /Jahr und /Mt)."""
    cat = _category_id("Optiker")
    m = f"{date.today():%Y-%m}"
    logged_in_client.post(
        "/budget/standard",
        data={"category_id": str(cat), "month": m, "amount": "1200", "interval": "jaehrlich"},
    )
    resp = logged_in_client.get("/budget")
    assert resp.status_code == 200
    assert "Rückstellungen" in resp.text
    assert "Optiker" in resp.text
    assert "/Jahr" in resp.text
    assert "/Mt" in resp.text


def test_monats_override_laesst_standard_soll_unangetastet(logged_in_client: TestClient) -> None:
    """Der Monats-Override gilt nur für diesen Monat und ersetzt den Standard nicht.

    Genau das war der Grund, ihn als eigenen Editor zu bauen statt als zweites
    Dauer-Eingabefeld: beide Beträge existieren nebeneinander, und man muss
    jederzeit zum Standard zurückkönnen.
    """
    from datetime import date

    from sqlalchemy import select

    from moneten.db.models import Budget, Category, ManagementType, StandardBudget
    from moneten.db.session import SessionLocal

    monat = date.today().replace(day=1)
    monat_str = f"{monat.year:04d}-{monat.month:02d}"

    with SessionLocal() as db:
        # Bewusst eine Unterkategorie unter einer BUDGET-relevanten Top-Kategorie:
        # Einkommen und Transfer werden auf der Budget-Seite gar nicht gerendert.
        eltern = db.scalars(
            select(Category).where(
                Category.parent_id.is_(None),
                Category.management_type.not_in([ManagementType.EINKOMMEN, ManagementType.TRANSFER]),
            )
        ).all()
        cat = None
        for top in eltern:
            cat = db.scalar(select(Category).where(Category.parent_id == top.id))
            if cat is not None:
                break
        assert cat is not None
        cat_id = cat.id
        db.add(StandardBudget(category_id=cat_id, amount=Decimal("300")))
        db.commit()

    # Editor öffnen — die Zeile muss das Override-Feld ausliefern.
    resp = logged_in_client.get(f"/budget?month={monat_str}&ovr={cat_id}")
    assert resp.status_code == 200
    assert 'name="soll"' in resp.text

    # Override setzen
    logged_in_client.post("/budget/set", data={
        "category_id": str(cat_id), "month": monat_str, "soll": "120",
    })
    with SessionLocal() as db:
        ovr = db.scalar(select(Budget).where(Budget.category_id == cat_id, Budget.month == monat))
        std = db.scalar(select(StandardBudget).where(StandardBudget.category_id == cat_id))
        assert ovr is not None and ovr.planned_amount == Decimal("120")
        assert std is not None and std.amount == Decimal("300"), "Standard-Soll wurde überschrieben"

    # Zurücksetzen (soll=0 löscht den Override)
    logged_in_client.post("/budget/set", data={
        "category_id": str(cat_id), "month": monat_str, "soll": "0",
    })
    with SessionLocal() as db:
        assert db.scalar(select(Budget).where(Budget.category_id == cat_id, Budget.month == monat)) is None
        std = db.scalar(select(StandardBudget).where(StandardBudget.category_id == cat_id))
        assert std is not None and std.amount == Decimal("300"), "Standard muss den Override überleben"


def test_anteil_prozent_rundet_und_kappt() -> None:
    """Anteil an den Monatsausgaben — ganze Prozent, nie über 100, nie negativ."""
    from moneten.services.budget_totals import anteil_prozent

    assert anteil_prozent(Decimal("1600"), Decimal("2400")) == 67   # 66.67 → 67
    assert anteil_prozent(Decimal("7.20"), Decimal("2400")) == 0    # 0.3 % rundet auf 0
    assert anteil_prozent(Decimal("50"), Decimal("0")) == 0         # kein Nenner
    assert anteil_prozent(Decimal("0"), Decimal("2400")) == 0
    assert anteil_prozent(Decimal("3000"), Decimal("2400")) == 100  # gekappt


def test_gruppe_ohne_soll_zeigt_anteil_statt_leerem_balken(logged_in_client: TestClient) -> None:
    """Eine Karte ohne gesetztes Soll hatte einen Balken auf 0 % — „0 von 0".

    Ohne Soll gibt es keinen Füllstand. Stattdessen trägt der Balken den Anteil
    der Gruppe an den Monatsausgaben; er ist immer definiert, sobald überhaupt
    etwas ausgegeben wurde, und ein Balken auf 0 % darf nirgends mehr vorkommen.
    """
    import re

    acc = _account_id()
    cat = _category_id("Hobby")  # „Freizeit & Persönlich" — ohne Standard-Soll
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        db.add(Transaction(account_id=acc, category_id=cat, date=m,
                           amount=Decimal("-88.00"), description="Erfundenes Hobby"))
        db.commit()

    resp = logged_in_client.get("/budget")
    assert resp.status_code == 200
    assert "% der Ausgaben" in resp.text
    assert "kein Soll" in resp.text

    breiten = [int(x) for x in re.findall(r'bgrp-anteil-bar">\s*<span style="width:(\d+)%', resp.text)]
    assert breiten, "kein einziger Anteilsbalken gerendert"
    assert all(b >= 1 for b in breiten), f"Balken auf 0 %: {breiten}"


def test_gruppe_ohne_soll_und_ohne_ausgaben_steht_hinter_dem_aufklapper(
    logged_in_client: TestClient,
) -> None:
    """Weder Soll noch Ausgabe: die Karte könnte nur ihren Namen zeigen."""
    resp = logged_in_client.get("/budget")
    assert "ohne Soll und ohne Ausgaben" in resp.text


def test_budgetseite_verweist_auf_die_abo_erkennung(logged_in_client: TestClient) -> None:
    """Warum stehen Versicherungen im Budget, erkannte Abos aber nicht?

    Weil das Budget SOLL-Werte führt, die der Nutzer selbst setzt, und die
    Abo-Seite wiederkehrende Zahlungen aus den Buchungen ERKENNT. Die Liste
    benennt beides und verlinkt die andere Seite.
    """
    resp = logged_in_client.get("/budget")
    assert "Selbst gesetztes Soll" in resp.text
    assert 'href="/subscriptions"' in resp.text
    assert "Erkannte Abos" in resp.text


def test_leitzahl_ohne_jedes_soll_meldet_keine_ueberschreitung() -> None:
    """Ohne ein einziges Soll ist „Rest" der negative Ist-Betrag.

    Die Leitzahl meldete dann rot „Über Budget CHF −2'397", obwohl nie ein
    Budget existierte. Der Zustand wird hier in einer Transaktion hergestellt
    und danach zurückgerollt — die Soll-Werte der übrigen Tests bleiben stehen.
    """
    from moneten.routers.budget import _build_view

    monat = date.today().replace(day=1)
    with SessionLocal() as db:
        for sb in db.scalars(select(StandardBudget)):
            db.delete(sb)
        for b in db.scalars(select(Budget)):
            db.delete(b)
        db.flush()  # nur in DIESER Transaktion sichtbar

        view = _build_view(db, monat)
        assert view["totals"]["kein_soll"] is True
        assert view["totals"]["soll"] == Decimal("0")

        db.rollback()

    # Nachweis, dass der Rollback gegriffen hat.
    with SessionLocal() as db:
        assert db.scalar(select(StandardBudget)) is not None
