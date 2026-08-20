"""Tests für die Kategorie-Verwaltung + Icon-Bibliothek."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Category, ManagementType, Transaction
from moneten.db.session import SessionLocal
from moneten.icons import ICONS


def test_icon_library_consistent() -> None:
    """Jeder Icon-Name in ICONS hat ein <symbol> im Sprite; ~75 Stück, eindeutig."""
    sprite = Path("src/moneten/templates/partials/icon_sprite.html").read_text(encoding="utf-8")
    names = [i["name"] for i in ICONS]
    assert len(names) == len(set(names)), "Doppelte Icon-Namen"
    # Obergrenze gegen Wildwuchs, kein Einfrieren: die Bibliothek ist bewusst
    # kuratiert (siehe Modul-Docstring von moneten.icons), und eine ganze
    # Fremdsammlung hereinzuziehen soll auffallen. Einzelne Ergänzungen für neue
    # Seiten sind erlaubt — „chart-line" kam mit der Verlaufsseite dazu.
    assert 70 <= len(names) <= 85, f"erwarte eine kuratierte Auswahl, sind {len(names)}"
    fehlend = [n for n in names if f'id="i-{n}"' not in sprite]
    assert not fehlend, f"Symbole fehlen im Sprite: {fehlend}"
    # Jedes Icon hat Stichwörter für die Suche.
    assert all(i.get("keywords") and i.get("label") for i in ICONS)


def test_template_icon_calls_registered() -> None:
    """Jeder literale ``icon('name')``-Aufruf in den Templates muss in ICONS
    registriert sein — sonst rendert ``icon()`` still das Fallback-Etikett
    (``#i-tag``). Fängt die Lücke, durch die dots/search/check zunächst auf
    'tag' fielen. (Dynamische ``icon(var)``-Aufrufe werden bewusst nicht erfasst.)"""
    import re

    names = {i["name"] for i in ICONS}
    tpl_dir = Path("src/moneten/templates")
    pattern = re.compile(r'''icon\(\s*['"]([a-z0-9-]+)['"]''')
    missing: dict[str, list[str]] = {}
    for path in tpl_dir.rglob("*.html"):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            if name not in names:
                missing.setdefault(name, []).append(path.name)
    assert not missing, f"icon('…') ohne ICONS-Eintrag → Fallback #i-tag: {missing}"


def test_rueckzahlungen_seeded() -> None:
    """Die Standard-Kategorie „Rückzahlungen" hängt unter „Freizeit & Persönlich"
    und zählt als Ausgabe (BARGELD)."""
    with SessionLocal() as db:
        c = db.scalars(select(Category).where(Category.name == "Rückzahlungen")).first()
        assert c is not None
        assert c.management_type == ManagementType.BARGELD
        parent = db.get(Category, c.parent_id)
        assert parent is not None and parent.name == "Freizeit & Persönlich"


def test_categories_page_renders(logged_in_client: TestClient) -> None:
    r = logged_in_client.get("/categories")
    assert r.status_code == 200
    assert "Deine Kategorien" in r.text  # Listen-Ansicht (Default)
    # Der Icon-Picker (mit Suche) erscheint im Anlege-Formular.
    form = logged_in_client.get("/categories?form=new").text
    assert "iconpick" in form and "iconpick-search" in form
    assert form.count("iconpick-opt") >= 70  # ~75 Icons im Picker


def _a_top_id() -> int:
    with SessionLocal() as db:
        return db.scalars(select(Category).where(Category.parent_id.is_(None))).first().id


def test_create_subcategory(logged_in_client: TestClient) -> None:
    top = _a_top_id()
    r = logged_in_client.post("/categories", data={
        "name": "Pokémon GO", "parent_id": str(top), "icon": "device-gamepad-2", "color": "#D97757", "art": "B",
    })
    assert r.status_code == 200
    with SessionLocal() as db:
        c = db.scalars(select(Category).where(Category.name == "Pokémon GO")).first()
        assert c is not None
        assert c.parent_id == top
        assert c.icon == "device-gamepad-2"
        assert c.color == "#D97757"
        # erbt die Art der Oberkategorie
        top_cat = db.get(Category, top)
        assert c.management_type == top_cat.management_type


def test_create_top_with_art(logged_in_client: TestClient) -> None:
    r = logged_in_client.post("/categories", data={
        "name": "Spezial-Topf", "parent_id": "", "icon": "target", "color": "", "art": "S",
    })
    assert r.status_code == 200
    with SessionLocal() as db:
        c = db.scalars(select(Category).where(Category.name == "Spezial-Topf")).first()
        assert c is not None and c.parent_id is None
        assert c.management_type == ManagementType.SPAREN


def test_move_subcategory_to_other_top(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        tops = db.scalars(select(Category).where(Category.parent_id.is_(None))).all()
        top_a, top_b = tops[0].id, tops[1].id
        sub = Category(name="Umhäng-Test", parent_id=top_a, management_type=tops[0].management_type)
        db.add(sub)
        db.commit()
        sub_id = sub.id
    r = logged_in_client.post(f"/categories/{sub_id}", data={
        "name": "Umhäng-Test", "parent_id": str(top_b), "icon": "tag", "color": "", "art": "",
    })
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.get(Category, sub_id).parent_id == top_b


def test_delete_blocked_with_transactions(logged_in_client: TestClient) -> None:
    from datetime import date
    from decimal import Decimal

    from moneten.db.models import Account, AccountType
    with SessionLocal() as db:
        acc = Account(name="C-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"))
        top = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
        cat = Category(name="Mit-Buchung", parent_id=top.id, management_type=top.management_type)
        db.add_all([acc, cat])
        db.flush()
        db.add(Transaction(account_id=acc.id, category_id=cat.id, date=date.today(),
                           amount=Decimal("-5"), description="x"))
        db.commit()
        cat_id = cat.id

    r = logged_in_client.post(f"/categories/{cat_id}/delete")
    assert r.status_code == 200
    assert "bitte stattdessen archivieren" in r.text
    with SessionLocal() as db:
        assert db.get(Category, cat_id) is not None  # nicht gelöscht


def test_archive_and_delete_empty(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        top = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
        cat = Category(name="Leer-Kat", parent_id=top.id, management_type=top.management_type)
        db.add(cat)
        db.commit()
        cat_id = cat.id
    # archivieren
    assert logged_in_client.post(f"/categories/{cat_id}/archive").status_code == 200
    with SessionLocal() as db:
        assert db.get(Category, cat_id).is_archived is True
    # löschen (keine Buchungen) → weg
    assert logged_in_client.post(f"/categories/{cat_id}/delete").status_code == 200
    with SessionLocal() as db:
        assert db.get(Category, cat_id) is None
