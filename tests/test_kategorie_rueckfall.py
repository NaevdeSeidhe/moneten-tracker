"""Unbekannte Beleg-Positionen erben die Kategorie des Ladens — lernen aber nichts daraus.

Gemessen an einem Migros-Bon mit neun Zeilen: drei blieben ohne Kategorie und
mussten von Hand gesetzt werden — bei jedem Einkauf dieselben Handgriffe.

Die Falle dabei: aus einer geerbten Kategorie eine Regel zu lernen hiesse, jeden
unbekannten Artikel dieses Ladens für immer als Lebensmittel zu führen — auch
die Kerze und das Waschmittel.

Alle Namen und Beträge sind erfunden.
"""

from __future__ import annotations

from sqlalchemy import select

from moneten.db.models import Category, ReceiptItemRule
from moneten.db.session import SessionLocal
from moneten.services.receipt_digital import analyze, learn
from moneten.services.receipt_ocr import OcrResult

BON = ("Migros\n"
       "Unbekannteswort Spezial 4.20\n"
       "Total CHF 4.20\n")


def _analyse(db):
    return analyze(db, OcrResult(text=BON, method="ocr", amount=None, date=None))


def test_unbekannte_position_erbt_die_ladenkategorie():
    """Sonst steht dort „Kategorie wählen" — bei jedem Einkauf neu."""
    with SessionLocal() as db:
        ergebnis = _analyse(db)
        posten = ergebnis["items"][0]
        assert posten["category_id"] is not None, "Position blieb ohne Kategorie"
        assert posten["category_auto_id"] == posten["category_id"]


def test_die_geerbte_kategorie_ist_als_solche_erkennbar():
    """Ohne dieses Feld liesse sich beim Speichern nicht sagen, ob jemand die
    Kategorie bestätigt hat oder ob sie bloss vom Laden stammt."""
    with SessionLocal() as db:
        posten = _analyse(db)["items"][0]
        assert "category_auto_id" in posten


def test_aus_einer_geerbten_kategorie_wird_nicht_gelernt():
    """Die eigentliche Absicherung.

    Würde daraus gelernt, trüge jeder unbekannte Artikel dieses Ladens für immer
    die Ladenkategorie — auch der, der nicht hineingehört.
    """
    with SessionLocal() as db:
        vorher = len(list(db.scalars(select(ReceiptItemRule))))
        quittung = _analyse(db)
        learn(db, quittung)
        db.commit()
        assert len(list(db.scalars(select(ReceiptItemRule)))) == vorher


def test_eine_geaenderte_kategorie_wird_gelernt():
    """Wer die Kategorie ändert, sagt etwas über den ARTIKEL — das ist eine Regel wert."""
    with SessionLocal() as db:
        quittung = _analyse(db)
        andere = db.scalar(
            select(Category).where(Category.id != quittung["items"][0]["category_id"])
        )
        quittung["items"][0]["category_id"] = andere.id
        vorher = len(list(db.scalars(select(ReceiptItemRule))))
        learn(db, quittung)
        db.commit()
        assert len(list(db.scalars(select(ReceiptItemRule)))) > vorher
        # Aufräumen: die Regel gehört nicht in die Testdatenbank der anderen Tests.
        for r in db.scalars(select(ReceiptItemRule).where(ReceiptItemRule.category_id == andere.id)):
            db.delete(r)
        db.commit()
