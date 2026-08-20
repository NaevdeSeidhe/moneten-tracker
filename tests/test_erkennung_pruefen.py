"""Der Beleg-Bestand als Prüfstand für die Erkennung.

Jeder gescannte Beleg hat seinen Rohtext behalten. Damit liegt in der App eine
Sammlung echter Belegtexte — quer über Läden und Papierqualitäten. Eine Änderung
am Parser liess sich vorher nur an dem einen Beleg prüfen, der gerade gemeldet
war; ob sie anderswo etwas kaputt machte, zeigte sich beim nächsten Scan.

Alle Texte und Beträge hier sind erfunden.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import Account, AccountType, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services import erkennung_pruefen

# Ein Beleg, den die heutige Erkennung sauber liest.
GUT = "Testladen\nBrot 3.50\nMilch 2.20\nTotal CHF 5.70\n"


def _leeren(db) -> None:
    for att in db.scalars(select(Attachment).where(Attachment.ocr_text.isnot(None))):
        db.delete(att)
    db.commit()
    for tx in db.scalars(select(Transaction).where(Transaction.description == "Pruefstand")):
        db.delete(tx)
    db.commit()


def _beleg(db, *, text: str, gespeichert: dict) -> int:
    konto = db.scalar(select(Account).where(Account.name == "Pruefstand-Konto"))
    if konto is None:
        konto = Account(name="Pruefstand-Konto", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"),
                        sort_order=904)
        db.add(konto)
        db.flush()
    tx = Transaction(account_id=konto.id, date=date(2026, 8, 16),
                     amount=Decimal("-5.70"), description="Pruefstand")
    db.add(tx)
    db.flush()
    att = Attachment(transaction_id=tx.id, file_path=None, original_name="Bon",
                     ocr_text=text,
                     parsed_items_json=json.dumps(gespeichert, ensure_ascii=False))
    db.add(att)
    db.commit()
    return att.id


def test_ein_falsch_gespeicherter_beleg_faellt_auf():
    """Der Fall, für den es die Seite gibt.

    Gespeichert ist eine Aufteilung, die den Total nicht ergibt — die wandert so
    in Budget und Preisverlauf. Aus demselben Rohtext liest die heutige
    Erkennung eine, die aufgeht.
    """
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, text=GUT, gespeichert={
            "merchant": "Testladen", "amount": "5.70",
            "items": [{"name": "Irgendwas", "price": "99.00"}],
        })
        befunde = erkennung_pruefen.pruefe(db)
        assert len(befunde) == 1
        assert befunde[0].alt_geht_auf is False
        assert befunde[0].neu_geht_auf is True
        assert befunde[0].gewonnen is True
        _leeren(db)


def test_eine_verschlechterung_wird_benannt():
    """Der Befund, der wehtun soll.

    Ein Beleg, dessen gespeicherte Aufteilung aufgeht, den die heutige Erkennung
    aber nicht mehr liest, ist ein Fehler IN DER ERKENNUNG — und den soll man
    sehen, bevor er im Alltag begegnet.
    """
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, text="Nur Rauschen ohne Zahlen\n", gespeichert={
            "merchant": "Testladen", "amount": "5.70",
            "items": [{"name": "Brot", "price": "3.50"}, {"name": "Milch", "price": "2.20"}],
        })
        befund = erkennung_pruefen.pruefe(db)[0]
        assert befund.alt_geht_auf is True
        assert befund.neu_geht_auf is False
        assert befund.verloren is True
        _leeren(db)


def test_die_bilanz_zaehlt_beide_seiten():
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, text=GUT, gespeichert={"merchant": "A", "amount": "5.70",
                                          "items": [{"name": "X", "price": "99.00"}]})
        _beleg(db, text=GUT, gespeichert={"merchant": "B", "amount": "5.70",
                                          "items": [{"name": "Brot", "price": "3.50"},
                                                    {"name": "Milch", "price": "2.20"}]})
        b = erkennung_pruefen.bilanz(erkennung_pruefen.pruefe(db))
        assert b.gesamt == 2
        assert b.alt_geht_auf == 1
        assert b.neu_geht_auf == 2
        assert b.gewonnen == 1
        assert b.verloren == 0
        _leeren(db)


def test_neu_auswerten_schreibt_die_positionen():
    with SessionLocal() as db:
        _leeren(db)
        att_id = _beleg(db, text=GUT, gespeichert={
            "merchant": "Testladen", "amount": "99.00",
            "items": [{"name": "Irgendwas", "price": "99.00"}],
        })
        assert erkennung_pruefen.neu_auswerten(db, [att_id]) == 1
        daten = json.loads(db.get(Attachment, att_id).parsed_items_json)
        assert [e["name"] for e in daten["items"]] == ["Brot", "Milch"]
        assert daten["amount"] == "5.70"
        _leeren(db)


def test_was_nicht_aufgeht_wird_nicht_geschrieben():
    """Eine Aufteilung, die den Total verfehlt, ersetzt keine andere, die es auch tut.

    Sonst bliebe es beim Raten, nur mit anderen Zahlen.
    """
    with SessionLocal() as db:
        _leeren(db)
        att_id = _beleg(db, text="Nur Rauschen\n", gespeichert={
            "merchant": "T", "amount": "5.70", "items": [{"name": "X", "price": "99.00"}],
        })
        assert erkennung_pruefen.neu_auswerten(db, [att_id]) == 0
        daten = json.loads(db.get(Attachment, att_id).parsed_items_json)
        assert daten["items"][0]["name"] == "X"   # unangetastet
        _leeren(db)


def test_die_seite_zeigt_den_befund(logged_in_client):
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, text=GUT, gespeichert={"merchant": "Musterladen", "amount": "5.70",
                                          "items": [{"name": "X", "price": "99.00"}]})
    r = logged_in_client.get("/settings/erkennung")
    assert r.status_code == 200
    assert "Musterladen" in r.text
    assert "zu holen" in r.text
    with SessionLocal() as db:
        _leeren(db)


def test_eine_position_gleich_dem_total_ist_keine_aufteilung():
    """Der Fall, der an einem echten Bestand aufflog — 13 Belege meldeten „1 Pos. ✓".

    Eine einzelne Position, die dem Total entspricht, ist keine Aufteilung: das
    ist die Totalzeile selbst, als Position gelesen. „Summe = Total" ist dann
    eine Identität, keine Probe — sie geht immer auf.

    Ohne diese Grenze schlug die Seite vor, 13 Belege „neu auszuwerten", und ein
    Klick hätte die Scheinposition hineingeschrieben. Dieselbe Regel gilt beim
    Lernen (``receipt_digital._nur_scheinbar_geprueft``); sie hier zu vergessen
    hiess, mit zweierlei Mass zu messen.
    """
    with SessionLocal() as db:
        _leeren(db)
        att_id = _beleg(db, text="Rechnung\nSchluesselanhaenger 45.00\nZu zahlen 45.00\n", gespeichert={
            "merchant": "PDF-Beleg", "amount": "45.00", "items": [],
        })
        befund = erkennung_pruefen.pruefe(db)[0]
        assert befund.neu_positionen == 1, "Testaufbau: es soll genau eine Position geben"
        assert befund.neu_geht_auf is False, "Scheinaufteilung gilt als stimmig"
        assert befund.gewonnen is False, "Sie wurde zum Neuauswerten vorgeschlagen"
        assert erkennung_pruefen.neu_auswerten(db, [att_id]) == 0
        _leeren(db)


def test_zwei_echte_positionen_gelten_weiter():
    """Die Verschärfung darf keine echte Aufteilung aussperren."""
    with SessionLocal() as db:
        _leeren(db)
        _beleg(db, text=GUT, gespeichert={"merchant": "T", "amount": "5.70", "items": []})
        assert erkennung_pruefen.pruefe(db)[0].neu_geht_auf is True
        _leeren(db)
