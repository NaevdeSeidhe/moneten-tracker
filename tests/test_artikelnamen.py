"""Die App merkt sich eine korrigierte Schreibweise — und wendet sie an.

Ohne das korrigiert man denselben Lesefehler bei jedem Beleg neu, und der
Preisverlauf führt eine Ware unter drei Namen: drei Verläufe mit je einem Punkt
statt einem mit dreien.

Alle Namen und Beträge sind erfunden.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select

from moneten.db.models import Account, AccountType, ArtikelAlias, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services import artikelnamen


def _leeren(db) -> None:
    for a in db.scalars(select(ArtikelAlias)):
        db.delete(a)
    for att in db.scalars(select(Attachment).where(Attachment.parsed_items_json.isnot(None))):
        db.delete(att)
    db.commit()
    for tx in db.scalars(select(Transaction).where(Transaction.description == "Beleg")):
        db.delete(tx)
    db.commit()


def _beleg(db, *namen: str) -> None:
    """Ein Anhang braucht eine Buchung — ``transaction_id`` ist NOT NULL."""
    from decimal import Decimal

    konto = db.scalar(select(Account).where(Account.name == "Namen-Konto"))
    if konto is None:
        konto = Account(name="Namen-Konto", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"),
                        sort_order=903)
        db.add(konto)
        db.flush()
    tx = Transaction(account_id=konto.id, date=date(2026, 8, 16),
                     amount=Decimal("-1.00"), description="Beleg")
    db.add(tx)
    db.flush()
    db.add(Attachment(
        transaction_id=tx.id, file_path=None, original_name="Test",
        parsed_items_json=json.dumps({"items": [{"name": n, "price": "1.00"} for n in namen]}),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Lernen
# ---------------------------------------------------------------------------


def test_eine_korrektur_wird_gemerkt():
    """Der Kern: einmal richtigstellen, danach stimmt es von selbst."""
    with SessionLocal() as db:
        _leeren(db)
        assert artikelnamen.lerne(db, "MUSTEBOL Kaps", "Musterol Kaps") is True
        db.commit()
        karte = artikelnamen.alias_karte(db)
        assert artikelnamen.anwenden("MUSTEBOL Kaps", karte) == "Musterol Kaps"
        _leeren(db)


def test_eine_umbenennung_wird_nicht_gemerkt():
    """Wer „Ware 3" zu „Milch" macht, meint diesen Beleg — keine Regel für alle.

    Ohne diese Grenze lernte die App aus jeder Korrektur eine Zuordnung, und
    zwei völlig verschiedene Artikel wären auf ewig verschmolzen.
    """
    with SessionLocal() as db:
        _leeren(db)
        assert artikelnamen.lerne(db, "Ware 3", "Milch Vollfett") is False
        assert artikelnamen.alias_karte(db) == {}
        _leeren(db)


def test_dieselbe_schreibweise_lernt_nichts():
    """„Bio Butter" und „Butter Bio" fallen auf denselben Schlüssel."""
    with SessionLocal() as db:
        _leeren(db)
        assert artikelnamen.lerne(db, "Bio Butter", "Butter Bio") is False
        _leeren(db)


# ---------------------------------------------------------------------------
# Bündeln und Bereinigen
# ---------------------------------------------------------------------------


def test_aehnliche_schreibweisen_landen_in_einem_buendel():
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, "Multifruchtsaft", "Multifruchtsaft")
        _beleg(db, "Muitifruchtsaft")
        gruppen = artikelnamen.buendel(db)
        assert len(gruppen) == 1
        assert gruppen[0].vorschlag == "Multifruchtsaft"   # die häufigere
        assert dict(gruppen[0].varianten) == {"Multifruchtsaft": 2, "Muitifruchtsaft": 1}
        _leeren(db)


def test_verschiedene_artikel_bleiben_getrennt():
    """Ähnlich heissen heisst nicht dasselbe sein."""
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, "Bio Butter", "Bio Joghurt")
        assert artikelnamen.buendel(db) == []
        _leeren(db)


def test_vereinheitlichen_schreibt_den_bestand_um():
    """Sonst führte der Preisverlauf die Ware weiter unter beiden Namen."""
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, "Muitifruchtsaft")
        _beleg(db, "Multifruchtsaft")
        geaendert = artikelnamen.vereinheitliche(db, "Multifruchtsaft", ["Muitifruchtsaft"])
        assert geaendert == 1
        namen = set()
        for att in db.scalars(select(Attachment).where(Attachment.parsed_items_json.isnot(None))):
            for e in json.loads(att.parsed_items_json)["items"]:
                namen.add(e["name"])
        assert namen == {"Multifruchtsaft"}
        _leeren(db)


def test_vereinheitlichen_merkt_sich_die_variante():
    """Der Bestand allein genügt nicht — der nächste Beleg bringt sie zurück."""
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, "Muitifruchtsaft")
        artikelnamen.vereinheitliche(db, "Multifruchtsaft", ["Muitifruchtsaft"])
        karte = artikelnamen.alias_karte(db)
        assert artikelnamen.anwenden("Muitifruchtsaft", karte) == "Multifruchtsaft"
        _leeren(db)


def test_die_seite_zeigt_die_buendel(logged_in_client):
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, "Multifruchtsaft")
        _beleg(db, "Muitifruchtsaft")
    r = logged_in_client.get("/settings/positionen")
    assert r.status_code == 200
    assert "Muitifruchtsaft" in r.text
    with SessionLocal() as db:
        _leeren(db)


# ---------------------------------------------------------------------------
# Der ganze Weg: Beleg speichern → Schreibweise sitzt
# ---------------------------------------------------------------------------


def test_gelernt_wird_auch_wenn_die_gegenprobe_scheitert():
    """Der Fall, der zählt — und der fast durchgerutscht wäre.

    Gelernt wurde zuerst NACH der Gegenprobe. Die leert die Positionen, wenn die
    Summe nicht aufgeht — also lernte die App ausgerechnet bei den Belegen
    nichts, die eine Korrektur brauchen. Der Apotheken-Bon mit einer falsch
    gelesenen Zeile geht nie auf.

    Dass „Musterol" richtig heisst, bleibt wahr, auch wenn die Summe der
    Positionen den Total verfehlt.
    """
    from moneten.services.receipt_digital import save_receipt

    with SessionLocal() as db:
        _leeren(db)
        quittung = {
            "merchant": "Apotheke Test", "amount": "17.25", "date": "2026-08-16",
            "items": [
                # Summe 30.00 gegen Total 17.25 — die Probe geht bewusst nicht auf.
                {"name": "Musterol Kaps", "name_ocr": "MUSTEBOL Kaps",
                 "price": "17.25", "category_id": None},
                {"name": "Zweites", "name_ocr": "Zweites", "price": "12.75",
                 "category_id": None},
            ],
        }
        save_receipt(db, quittung, "Rohtext", source="photo")
        karte = artikelnamen.alias_karte(db)
        assert artikelnamen.anwenden("MUSTEBOL Kaps", karte) == "Musterol Kaps"
        _leeren(db)


def test_der_naechste_beleg_zeigt_die_richtige_schreibweise():
    """Was die Korrektur wert ist: beim nächsten Mal steht es schon da."""
    from moneten.services.receipt_digital import analyze
    from moneten.services.receipt_ocr import OcrResult

    with SessionLocal() as db:
        _leeren(db)
        artikelnamen.lerne(db, "MUSTEBOL Kaps", "Musterol Kaps")
        db.commit()
        ergebnis = analyze(db, OcrResult(
            text="Apotheke\nMUSTEBOL Kaps 17.25\nTotal 17.25\n",
            method="ocr", amount=None, date=None))
        namen = [e["name"] for e in ergebnis["items"]]
        assert namen == ["Musterol Kaps"], namen
        # Was die Erkennung wirklich las, bleibt am Eintrag — sonst liesse sich
        # eine falsch gelernte Zuordnung später nicht mehr nachvollziehen.
        assert ergebnis["items"][0]["name_ocr"] == "MUSTEBOL Kaps"
        _leeren(db)
