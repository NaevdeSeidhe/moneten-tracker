"""Hygiene-Befunde: was die Aufräum-Ansicht meldet — und was nicht.

Der Dienst schlägt Dinge zum Löschen vor. Ein falscher Befund kostet hier mehr
als ein fehlender: wer einer Empfehlung folgt und eine benutzte Kategorie
löscht, verliert die Zuordnung seiner Buchungen.

Alle Daten hier sind erfunden und im Test selbst nachlesbar.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import Account, Category, Transaction, TransactionSplit
from moneten.db.session import SessionLocal
from moneten.services.hygiene import hygiene_befunde

HEUTE = date(2026, 7, 26)


def _unterkategorie(db, name: str) -> Category:
    top = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
    c = Category(name=name, parent_id=top.id, sort_order=900,
                 management_type=top.management_type)
    db.add(c)
    db.flush()
    return c


def test_kategorie_nur_aus_splits_gilt_nicht_als_eingeschlafen() -> None:
    """Regressionstest: Splits tragen am Buchungskopf keine Kategorie.

    Ohne eigene Split-Abfrage sieht jede Kategorie, die nur über Beleg-
    Aufteilungen bebucht wird, wie seit Jahren unbenutzt aus — die Aufräum-
    Ansicht schlüge ausgerechnet die zum Löschen vor, die der Quittungs-Scan am
    fleissigsten füllt.
    """
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        kat = _unterkategorie(db, f"ZZZnursplit{marke}")
        tx = Transaction(account_id=konto.id, category_id=None, is_split=True,
                         date=date(2026, 7, 2), amount=Decimal("-50"),
                         description="ZZZ Bon")
        db.add(tx)
        db.flush()
        split = TransactionSplit(transaction_id=tx.id, category_id=kat.id,
                                 amount=Decimal("-50"))
        db.add(split)
        db.commit()

        namen = {e["kategorie"].name for e in hygiene_befunde(db, HEUTE)["eingeschlafen"]}
        assert kat.name not in namen, (
            "Kategorie wird nur über Splits bebucht, gilt aber als eingeschlafen"
        )

        db.delete(split)
        db.delete(tx)
        db.delete(kat)
        db.commit()


def test_wirklich_unbenutzte_kategorie_wird_gemeldet() -> None:
    """Gegenprobe: der Befund darf nicht einfach nie auslösen."""
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        kat = _unterkategorie(db, f"ZZZungenutzt{marke}")
        db.commit()

        befunde = hygiene_befunde(db, HEUTE)["eingeschlafen"]
        treffer = [e for e in befunde if e["kategorie"].name == kat.name]
        assert treffer, "Eine nie bebuchte Kategorie gehört in die Aufräum-Liste"
        assert treffer[0]["zuletzt"] is None

        db.delete(kat)
        db.commit()


def test_frisch_bebuchte_kategorie_bleibt_unauffaellig() -> None:
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        kat = _unterkategorie(db, f"ZZZaktiv{marke}")
        tx = Transaction(account_id=konto.id, category_id=kat.id,
                         date=date(2026, 7, 10), amount=Decimal("-20"),
                         description="ZZZ frisch")
        db.add(tx)
        db.commit()

        namen = {e["kategorie"].name for e in hygiene_befunde(db, HEUTE)["eingeschlafen"]}
        assert kat.name not in namen

        db.delete(tx)
        db.delete(kat)
        db.commit()
