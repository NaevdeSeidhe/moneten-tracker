"""Dieselbe Frage, dieselbe Antwort — egal auf welcher Seite.

Der Monatsrest steht an zwei Orten: als Leitzahl „Noch frei" auf der Budget-Seite
und als „Budget-Rest (Monat)" auf der Schnell-Erfassen-Seite, die am Handy oft
die erste ist, die man sieht. Beide rechneten ihn früher getrennt und
verschieden — sichtbar wurde das erst, wenn unkategorisierte Buchungen im Spiel
waren: die zählten auf ``/quick`` voll gegen das Budget, auf ``/budget`` gar nicht.

Diese Tests sind der Grund, warum die Rechnung in einem gemeinsamen Service
liegt. Ohne sie wäre der Service nur eine Absichtserklärung.
"""

from __future__ import annotations

import html
import re
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, Transaction
from moneten.db.session import SessionLocal
from moneten.services.budget_totals import monats_totale


def _zahl(text: str) -> Decimal:
    """„CHF −1&#39;234.50" → Decimal.

    Der ``chf``-Filter setzt typografische Minus- und Hochkommata, und Jinja
    escapet Letztere zu ``&#39;`` — beides muss raus, bevor Decimal es sieht.
    """
    bereinigt = (
        html.unescape(text)
        .replace("−", "-").replace("–", "-")
        .replace("'", "").replace("’", "")
        .replace("CHF", "").replace(" ", "").strip()
    )
    return Decimal(bereinigt)


def _leitzahl_budget(client: TestClient) -> Decimal:
    seite = client.get("/budget").text
    m = re.search(r'class="budget-hero-num"[^>]*data-countup="(-?[\d.]+)"', seite)
    assert m, "Leitzahl der Budget-Seite nicht gefunden"
    return Decimal(m.group(1))


def _rest_quick(client: TestClient) -> Decimal | None:
    seite = client.get("/quick").text
    m = re.search(
        r'Budget-Rest \(Monat\)</div>\s*<div class="metric-value"[^>]*>([^<]+)</div>',
        seite,
    )
    if not m:
        return None
    return _zahl(m.group(1))


@contextmanager
def _mit_soll(betrag: str = "500"):
    """Ein Standard-Soll auf einer Ausgaben-Kategorie.

    Ohne Soll zeigt ``/quick`` gar keinen Rest (``rest_month`` ist dann ``None``)
    und es gäbe nichts zu vergleichen. Der Test schafft sich die Voraussetzung
    selbst, statt auf Seed-Daten zu hoffen, die es je nach Lauf gibt oder nicht.
    """
    from moneten.db.models import Category, ManagementType, StandardBudget

    with SessionLocal() as db:
        top = db.scalars(
            select(Category).where(
                Category.parent_id.is_(None),
                Category.management_type.not_in(
                    [ManagementType.EINKOMMEN, ManagementType.TRANSFER]
                ),
            )
        ).first()
        assert top is not None, "Keine Ausgaben-Oberkategorie in der Test-DB"
        kat = db.scalars(
            select(Category).where(
                Category.parent_id == top.id, Category.is_archived.is_(False)
            )
        ).first()
        assert kat is not None, f"Keine Unterkategorie unter {top.name}"

        vorhanden = db.scalars(
            select(StandardBudget).where(StandardBudget.category_id == kat.id)
        ).first()
        angelegt = None
        if vorhanden is None:
            angelegt = StandardBudget(category_id=kat.id, amount=Decimal(betrag))
            db.add(angelegt)
            db.commit()
        sb_id = angelegt.id if angelegt else None
    try:
        yield
    finally:
        if sb_id is not None:
            with SessionLocal() as db:
                db.delete(db.get(StandardBudget, sb_id))
                db.commit()


def test_beide_seiten_zeigen_denselben_monatsrest(logged_in_client: TestClient) -> None:
    """Die Kernzusicherung: eine Zahl, zwei Anzeigen, kein Unterschied."""
    with _mit_soll():
        budget = _leitzahl_budget(logged_in_client)
        quick = _rest_quick(logged_in_client)
    assert quick is not None, "Auf /quick steht kein Budget-Rest"
    assert quick == budget, (
        f"/budget zeigt {budget}, /quick zeigt {quick} — dieselbe Frage, zwei Antworten"
    )


def test_unkategorisierte_buchung_verschiebt_beide_gleich(
    logged_in_client: TestClient,
) -> None:
    """Regressionstest für den konkreten Unterschied.

    Eine Buchung ohne Kategorie zählte auf ``/quick`` voll gegen das Budget, auf
    ``/budget`` gar nicht. Wie die App damit umgeht, ist eine Design-Frage —
    dass sie es auf beiden Seiten gleich tut, ist keine.
    """
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        tx = Transaction(account_id=konto.id, category_id=None,
                         date=date.today(), amount=Decimal("-77"),
                         description=f"ZZZohnekat{marke}")
        db.add(tx)
        db.commit()
        tx_id = tx.id

    try:
        with _mit_soll():
            budget = _leitzahl_budget(logged_in_client)
            quick = _rest_quick(logged_in_client)
        assert quick == budget, (
            f"Mit einer unkategorisierten Buchung im Monat: /budget {budget}, /quick {quick}"
        )
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Transaction, tx_id))
            db.commit()


def test_rest_ist_soll_minus_ist() -> None:
    """Die einfachste Eigenschaft der Rechnung — sie muss trotzdem gelten."""
    with SessionLocal() as db:
        t = monats_totale(db, date.today().replace(day=1))
    assert t["rest"] == t["soll"] - t["ist"]
    assert t["soll"] >= 0 and t["ist"] >= 0
