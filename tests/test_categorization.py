"""Tests für die Auto-Kategorisierung (Regel-Engine)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    Category,
    CategoryRule,
    ManagementType,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services.categorization import (
    apply_rules,
    learn_from_transaction,
    match_category,
    seed_starter_rules,
    suggest_keyword,
    uncategorized_groups,
)


# ----------  Match-Logik (rein, deterministisch)  ----------
def test_match_category_substring_caseinsensitive() -> None:
    pairs = [("coop", 10), ("migros", 20)]
    assert match_category(pairs, "EINKAUF COOP CITY MUSTERSTADT") == 10
    assert match_category(pairs, "migros mm musterstadt") == 20
    assert match_category(pairs, "Tankstelle Avia") is None


def test_match_category_first_rule_wins() -> None:
    pairs = [("musterhausen", 99), ("migros", 20)]   # der erste Eintrag steht zuerst
    assert match_category(pairs, "migros musterhausen") == 99


# ----------  Starter-Set  ----------
def test_seed_starter_rules_idempotent() -> None:
    # Beim Test-Setup (seed_all) sind die Starter-Regeln bereits vorhanden.
    with SessionLocal() as db:
        assert seed_starter_rules(db) == 0  # nichts Neues
        coop = db.scalar(select(CategoryRule).where(CategoryRule.keyword == "coop"))
        assert coop is not None


# ----------  Anwendung: setzt Kategorie, respektiert manuelle Zuordnung  ----------
def test_apply_rules_sets_and_respects_manual() -> None:
    with SessionLocal() as db:
        acc = Account(name="Rule-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=950)
        db.add(acc)
        db.flush()
        gaming_id = db.scalar(select(Category.id).where(Category.name == "Gaming"))
        kaffee_id = db.scalar(select(Category.id).where(Category.name == "Kaffee"))
        db.add(CategoryRule(keyword="zzztestmerchant", category_id=gaming_id, sort_order=1))
        m = date.today().replace(day=1)
        t1 = Transaction(account_id=acc.id, date=m, amount=Decimal("-10.00"),
                         description="Kauf ZZZtestMerchant Filiale")          # unkategorisiert → soll zugeordnet werden
        t2 = Transaction(account_id=acc.id, category_id=kaffee_id, date=m, amount=Decimal("-5.00"),
                         description="zzztestmerchant abo")                    # manuell → darf NICHT überschrieben werden
        db.add_all([t1, t2])
        db.commit()
        t1id, t2id = t1.id, t2.id

    with SessionLocal() as db:
        apply_rules(db, only_uncategorized=True)

    with SessionLocal() as db:
        assert db.get(Transaction, t1id).category_id == gaming_id   # per Regel zugeordnet
        assert db.get(Transaction, t2id).category_id == kaffee_id   # manuelle Kategorie unangetastet


# ----------  Seite + Endpoints  ----------
def test_rules_page_and_add(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/rules")
    assert resp.status_code == 200
    assert "Kategorisierungs-Regeln" in resp.text

    cat_id = db_gaming = None
    with SessionLocal() as db:
        db_gaming = db.scalar(select(Category.id).where(Category.name == "Alkohol"))
        cat_id = db_gaming
    resp = logged_in_client.post("/rules", data={"keyword": "zzzbarshop", "category_id": str(cat_id)})
    assert resp.status_code == 200
    with SessionLocal() as db:
        r = db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzbarshop"))
        assert r is not None and r.category_id == cat_id


def test_extra_categories_seeded() -> None:
    """Technik/Snacks/Haushalt werden beim Seeding angelegt."""
    with SessionLocal() as db:
        names = {c.name for c in db.scalars(select(Category))}
    assert {"Technik", "Snacks", "Haushalt"} <= names


def test_apply_rules_marks_bargeldbezug_as_transfer() -> None:
    """Eine Buchung, die in eine Transfer-Kategorie fällt (Bargeldbezug),
    wird zusätzlich als management_type=TRANSFER markiert (kein Aufwand)."""
    with SessionLocal() as db:
        acc = Account(name="Transfer-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=960)
        db.add(acc)
        db.flush()
        bargeld_id = db.scalar(select(Category.id).where(Category.name == "Bargeldbezug"))
        db.add(CategoryRule(keyword="zzzbancomat", category_id=bargeld_id, sort_order=1))
        m = date.today().replace(day=1)
        tx = Transaction(account_id=acc.id, date=m, amount=Decimal("-200.00"),
                         description="ATM ZZZbancomat Bezug")
        db.add(tx)
        db.commit()
        txid = tx.id

    with SessionLocal() as db:
        apply_rules(db, only_uncategorized=True)

    with SessionLocal() as db:
        t = db.get(Transaction, txid)
        assert t.category_id == bargeld_id
        assert t.management_type == ManagementType.TRANSFER


# ----------  Lernen aus manueller Kategorisierung  ----------
def test_suggest_keyword_skips_noise() -> None:
    assert suggest_keyword("Gutschrift planikum ag") == "planikum"
    assert suggest_keyword("TWINT Beispielshopp Beispielshop") == "beispielshopp"
    assert suggest_keyword("EINKAUF COOP CITY MUSTERSTADT") == "coop"
    assert suggest_keyword("") == ""


def test_learn_from_transaction_creates_rule_and_applies() -> None:
    with SessionLocal() as db:
        acc = Account(name="Learn-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=970)
        db.add(acc)
        db.flush()
        gaming_id = db.scalar(select(Category.id).where(Category.name == "Gaming"))
        kaffee_id = db.scalar(select(Category.id).where(Category.name == "Kaffee"))
        m = date.today().replace(day=1)
        src = Transaction(account_id=acc.id, category_id=gaming_id, date=m, amount=Decimal("-10.00"),
                          description="ZZZlearnshop Kauf 1")            # die manuell gesetzte Quelle
        other = Transaction(account_id=acc.id, date=m, amount=Decimal("-20.00"),
                            description="zzzlearnshop kauf 2")          # unkategorisiert → soll mitgezogen werden
        manual = Transaction(account_id=acc.id, category_id=kaffee_id, date=m, amount=Decimal("-5.00"),
                             description="zzzlearnshop abo")            # manuell → unangetastet
        db.add_all([src, other, manual])
        db.commit()
        src_id, other_id, manual_id = src.id, other.id, manual.id

    with SessionLocal() as db:
        created, applied = learn_from_transaction(
            db, keyword="zzzlearnshop", category_id=gaming_id, source_tx_id=src_id
        )
        assert created is True
        assert applied == 1   # nur 'other' (manual ist kategorisiert, src ist die Quelle)

    with SessionLocal() as db:
        assert db.get(Transaction, other_id).category_id == gaming_id
        assert db.get(Transaction, manual_id).category_id == kaffee_id  # manuell bleibt
        assert db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzlearnshop")) is not None

    # Zweiter Aufruf: Regel existiert bereits → created False
    with SessionLocal() as db:
        created2, _ = learn_from_transaction(db, keyword="zzzlearnshop", category_id=gaming_id)
        assert created2 is False


def test_update_transaction_learns_rule(logged_in_client: TestClient) -> None:
    """Über das Bearbeiten-Formular mit ‚Zuordnung merken' wird die Regel gelernt
    und gleich auf weitere Buchungen angewandt (serverseitig)."""
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        acc = Account(name="Route-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=980)
        db.add(acc)
        db.flush()
        gaming_id = db.scalar(select(Category.id).where(Category.name == "Gaming"))
        a = Transaction(account_id=acc.id, date=m, amount=Decimal("-10.00"), description="ZZZrouteshop A")
        b = Transaction(account_id=acc.id, date=m, amount=Decimal("-12.00"), description="zzzrouteshop B")
        db.add_all([a, b])
        db.commit()
        a_id, b_id, acc_id = a.id, b.id, acc.id

    resp = logged_in_client.post(f"/transactions/{a_id}", data={
        "kind": "ausgabe", "amount": "10.00", "date": m.isoformat(),
        "account_id": str(acc_id), "category_id": str(gaming_id),
        "description": "ZZZrouteshop A", "notes": "",
        "learn_rule": "1", "learn_keyword": "zzzrouteshop",
    })
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.get(Transaction, a_id).category_id == gaming_id
        assert db.get(Transaction, b_id).category_id == gaming_id  # via gelernte Regel


# ----------  Schnell-Zuordnen-Inbox: Gruppieren + Vorschlag + Route  ----------
def test_uncategorized_groups_groups_and_suggests() -> None:
    """Unkategorisierte Buchungen werden nach Händler-Stichwort gruppiert; eine
    bestehende Regel auf den Text liefert den Kategorie-Vorschlag."""
    with SessionLocal() as db:
        acc = Account(name="Inbox-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=985)
        db.add(acc)
        db.flush()
        gaming_id = db.scalar(select(Category.id).where(Category.name == "Gaming"))
        # Regel sorgt für den Vorschlag (greift per Substring auf den Buchungstext).
        db.add(CategoryRule(keyword="zzzinboxmarkt", category_id=gaming_id, sort_order=1))
        m = date.today().replace(day=1)
        for i in range(3):
            db.add(Transaction(account_id=acc.id, date=m, amount=Decimal(f"-{10 + i}.00"),
                               description=f"ZZZinboxmarkt Filiale {i}"))
        db.commit()
        acc_id = acc.id

    with SessionLocal() as db:
        groups = uncategorized_groups(db)
        grp = next((g for g in groups if g.keyword == "zzzinboxmarkt"), None)
        assert grp is not None
        assert grp.count == 3
        # Netto MIT Vorzeichen: drei Ausgaben ergeben eine negative Summe.
        # Vorher stand hier die Summe der Betraege OHNE Vorzeichen — eine Gruppe
        # mit Ein- und Ausgaengen zeigte dadurch eine Zahl, die es nicht gibt.
        assert grp.total == Decimal("-33.00")           # -10-11-12
        assert grp.gemischt is False                     # hier nur Ausgaben
        assert grp.suggested_category_id == gaming_id    # aus der Regel vorgeschlagen

    # Aufräumen, damit andere Tests nicht über diese Buchungen stolpern.
    with SessionLocal() as db:
        for t in db.scalars(select(Transaction).where(Transaction.account_id == acc_id)):
            db.delete(t)
        db.commit()


def test_assign_group_route_assigns_all_and_learns(logged_in_client: TestClient) -> None:
    """POST /rules/assign-group ordnet die ganze Gruppe zu und legt eine Regel an."""
    m = date.today().replace(day=1)
    with SessionLocal() as db:
        acc = Account(name="Inbox-Route-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=986)
        db.add(acc)
        db.flush()
        snacks_id = db.scalar(select(Category.id).where(Category.name == "Snacks"))
        a = Transaction(account_id=acc.id, date=m, amount=Decimal("-9.00"), description="ZZZkiosk A")
        b = Transaction(account_id=acc.id, date=m, amount=Decimal("-4.00"), description="zzzkiosk B")
        db.add_all([a, b])
        db.commit()
        a_id, b_id = a.id, b.id

    resp = logged_in_client.post("/rules/assign-group",
                                 data={"keyword": "zzzkiosk", "category_id": str(snacks_id)})
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.get(Transaction, a_id).category_id == snacks_id
        assert db.get(Transaction, b_id).category_id == snacks_id
        assert db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzkiosk")) is not None


def test_rules_bulk_add(logged_in_client: TestClient) -> None:
    text = "zzzbulkshop = Gaming\nzzzbulk2 = Kaffee\n# Kommentar\nzzzbad = GibtsNichtKategorie"
    resp = logged_in_client.post("/rules/bulk", data={"text": text})
    assert resp.status_code == 200
    assert "Unbekannte Kategorien" in resp.text   # die ungültige Zeile wird gemeldet
    with SessionLocal() as db:
        assert db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzbulkshop")) is not None
        assert db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzbulk2")) is not None
        assert db.scalar(select(CategoryRule).where(CategoryRule.keyword == "zzzbad")) is None
