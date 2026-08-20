"""Verifiziert, dass Seeds Konten und die Kategorien-Hierarchie liefern."""

from __future__ import annotations

from sqlalchemy import select

from moneten.db.models import Account, Category
from moneten.db.session import SessionLocal


def test_accounts_seeded() -> None:
    with SessionLocal() as db:
        accounts = db.scalars(select(Account)).all()
    names = {a.name for a in accounts}
    assert {"Privatkonto", "Geldkassette", "Sparkonto", 'Sparkonto "Ferien"', "Säule 3a"} <= names
    # Crypto + Aktien sind als inaktive Platzhalter vorhanden.
    inactive = {a.name for a in accounts if not a.is_active}
    assert {"Crypto", "Aktien"} <= inactive


def test_categories_hierarchical() -> None:
    with SessionLocal() as db:
        top = db.scalars(select(Category).where(Category.parent_id.is_(None))).all()
        all_cats = db.scalars(select(Category)).all()
    top_names = {c.name for c in top}
    # Stichprobe gemäss Abschnitt 6.
    for required in ("Einnahmen", "Wohnen", "Konsum", "Abos", "Sparen & Vorsorge", "Transfer"):
        assert required in top_names, f"Top-Level fehlt: {required}"
    # Es muss deutlich mehr Sub- als Top-Kategorien geben.
    assert len(all_cats) > len(top) * 2

    # Wenigstens eine Abo-Kategorie hat is_subscription gesetzt.
    subs = [c for c in all_cats if c.is_subscription]
    assert any(c.name == "KI-Dienste" for c in subs)
