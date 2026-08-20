"""Tests für die Konten-Verwaltung (Phase 1)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_accounts_page_requires_login(client: TestClient) -> None:
    resp = client.get("/accounts", follow_redirects=False)
    assert resp.status_code in (303, 307)


def test_accounts_list_shows_seeded(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/accounts")
    assert resp.status_code == 200
    # Seed-Konten sind sichtbar.
    assert "Privatkonto" in resp.text
    assert "Geldkassette" in resp.text


def test_account_create(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/accounts",
        data={"name": "Testkonto", "type": "bank", "currency": "CHF", "balance": "1'234.50", "iban": ""},
    )
    assert resp.status_code == 200
    assert "Testkonto" in resp.text
    # Saldo wurde korrekt geparst und formatiert. Der Tausender-Apostroph wird
    # von Jinja als &#39; escaped (im Browser sichtbar als '), daher prüfen wir
    # den numerischen Teil.
    assert "234.50" in resp.text


def test_account_create_validation(logged_in_client: TestClient) -> None:
    # Leerer Name -> Fehler, kein neues Konto.
    resp = logged_in_client.post(
        "/accounts",
        data={"name": "  ", "type": "bank", "currency": "CHF", "balance": "0"},
    )
    assert resp.status_code == 400
    assert "Namen" in resp.text

    # Ungültiger Saldo -> Fehler.
    resp = logged_in_client.post(
        "/accounts",
        data={"name": "Kaputt", "type": "bank", "currency": "CHF", "balance": "abc"},
    )
    assert resp.status_code == 400
    assert "gültige Zahl" in resp.text


def test_account_edit_and_balance(logged_in_client: TestClient) -> None:
    # Konto anlegen
    logged_in_client.post(
        "/accounts",
        data={"name": "EditMich", "type": "savings", "currency": "CHF", "balance": "100"},
    )
    # ID herausfinden über die Edit-Form (wir nehmen das zuletzt angelegte via DB)
    from sqlalchemy import select

    from moneten.db.models import Account
    from moneten.db.session import SessionLocal

    with SessionLocal() as db:
        acc = db.scalar(select(Account).where(Account.name == "EditMich"))
        assert acc is not None
        acc_id = acc.id

    # Saldo ändern
    resp = logged_in_client.post(
        f"/accounts/{acc_id}",
        data={"name": "EditMich", "type": "savings", "currency": "CHF", "balance": "555.55"},
    )
    assert resp.status_code == 200
    assert "555.55" in resp.text

    with SessionLocal() as db:
        acc = db.get(Account, acc_id)
        assert str(acc.current_balance) == "555.55"


def test_account_toggle_and_delete(logged_in_client: TestClient) -> None:
    logged_in_client.post(
        "/accounts",
        data={"name": "Wegwerf", "type": "cash", "currency": "CHF", "balance": "0"},
    )
    from sqlalchemy import select

    from moneten.db.models import Account
    from moneten.db.session import SessionLocal

    with SessionLocal() as db:
        acc_id = db.scalar(select(Account).where(Account.name == "Wegwerf")).id

    # Archivieren
    resp = logged_in_client.post(f"/accounts/{acc_id}/toggle")
    assert resp.status_code == 200
    assert "archiviert" in resp.text

    # Löschen
    resp = logged_in_client.post(f"/accounts/{acc_id}/delete")
    assert resp.status_code == 200
    assert "Wegwerf" not in resp.text


def test_account_delete_blocked_with_transactions(logged_in_client: TestClient) -> None:
    """Konto mit Buchungen darf NICHT löschbar sein: der RESTRICT-FK würde sonst
    erst beim Commit knallen (IntegrityError → 500). Erwartet: 400 + verständliche
    Meldung, Konto bleibt bestehen."""
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    from moneten.db.models import Account, Transaction
    from moneten.db.session import SessionLocal

    logged_in_client.post(
        "/accounts",
        data={"name": "MitBuchung", "type": "cash", "currency": "CHF", "balance": "0"},
    )
    with SessionLocal() as db:
        acc_id = db.scalar(select(Account).where(Account.name == "MitBuchung")).id
        db.add(Transaction(account_id=acc_id, date=date(2026, 7, 1),
                           amount=Decimal("-5.00"), description="Testkauf"))
        db.commit()

    resp = logged_in_client.post(f"/accounts/{acc_id}/delete")
    assert resp.status_code == 400
    assert "archivieren" in resp.text
    with SessionLocal() as db:
        assert db.get(Account, acc_id) is not None  # Konto lebt noch


def test_anteile_und_zwischensummen_ignorieren_archivierte_konten(
    logged_in_client: TestClient,
) -> None:
    """Archivierte Konten dürfen die Bezugsgrösse nicht verzerren.

    Regressionstest: `total` (Leitzahl + Nenner der Anteile) zählte nur aktive
    Konten, die Gruppen-Zwischensumme dagegen alle. Ein archiviertes Konto mit
    Saldo liess damit die Anteile über 100 % summieren und die Zwischensummen
    ergaben nicht mehr die Leitzahl darüber.
    """
    from decimal import Decimal

    from moneten.db.models import Account, AccountType
    from moneten.db.session import SessionLocal
    from moneten.routers.accounts import _view_context

    with SessionLocal() as db:
        aktiv = Account(name="ZZZ-Aktiv", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("1000"), current_balance=Decimal("1000"),
                        sort_order=960, is_active=True)
        archiviert = Account(name="ZZZ-Archiviert", type=AccountType.BANK, currency="CHF",
                             opening_balance=Decimal("500"), current_balance=Decimal("500"),
                             sort_order=961, is_active=False)
        db.add_all([aktiv, archiviert])
        db.commit()

    with SessionLocal() as db:
        ctx = _view_context(db)

    # `nw_now`, nicht `total_balance`: gerendert wird nur ersteres
    # (partials/accounts_root.html, .nw-value). Der Test prüfte vorher
    # `total_balance` — einen Kontext-Schlüssel, den kein Template ausgibt.
    # Er blieb deshalb grün, während die tatsächlich angezeigte Leitzahl aus
    # `net_worth_series` kam und archivierte Konten mitzählte.
    total = ctx["nw_now"]
    summe_gruppen = sum(g["subtotal"] for g in ctx["acc_groups"])
    assert summe_gruppen == total, (
        "Die Gruppen-Zwischensummen müssen zusammen die angezeigte Leitzahl ergeben "
        f"(Leitzahl {total}, Gruppen {summe_gruppen})"
    )
    assert ctx["total_balance"] == ctx["nw_now"], (
        "Zwei Kontext-Werte für dieselbe Grösse dürfen nicht auseinanderlaufen"
    )

    zeilen = [r for g in ctx["acc_groups"] for r in g["accounts"]]
    archiv_zeile = next(r for r in zeilen if r["acc"].name == "ZZZ-Archiviert")
    assert archiv_zeile["share"] == 0, "Archiviertes Konto trägt keinen Anteil"

    summe_anteile = sum(r["share"] for r in zeilen)
    assert summe_anteile <= 100.5, f"Anteile summieren auf {summe_anteile} %"
