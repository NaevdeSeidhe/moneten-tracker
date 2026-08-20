"""Das Scan-Protokoll: was die Erkennung gesehen hat, bleibt nachvollziehbar.

Ohne dieses Protokoll war der Rohtext nur im offenen Dialog zu haben. Fenster
zu, Text weg — und ein Erkennungsfehler liess sich nur nachstellen, indem man
den Beleg abfotografierte und das Bild schickte. Für jeden Fall einzeln.

Alle Texte und Beträge hier sind erfunden.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import ScanProtokoll
from moneten.db.session import SessionLocal
from moneten.services import scan_protokoll


def _leeren(db) -> None:
    for e in db.scalars(select(ScanProtokoll)):
        db.delete(e)
    db.commit()


def _quittung(haendler="Testladen", betrag="12.30", posten=2) -> dict:
    return {"merchant": haendler, "amount": betrag,
            "items": [{"name": f"Ware {i}", "price": "1.00"} for i in range(posten)]}


def test_ein_scan_hinterlaesst_seinen_rohtext():
    with SessionLocal() as db:
        _leeren(db)
        scan_protokoll.protokolliere(db, quittung=_quittung(),
                                     ocr_text="Testladen\nWare 1 1.00\nTotal 12.30",
                                     methode="ocr")
        eintraege = scan_protokoll.letzte(db)
        assert len(eintraege) == 1
        assert eintraege[0].haendler == "Testladen"
        assert eintraege[0].betrag == Decimal("12.30")
        assert eintraege[0].positionen == 2
        assert "Total 12.30" in eintraege[0].ocr_text
        _leeren(db)


def test_das_protokoll_waechst_nicht_unbegrenzt():
    """Ein Protokoll, das ewig wächst, ist ein Datenlager statt eines Werkzeugs."""
    with SessionLocal() as db:
        _leeren(db)
        for i in range(scan_protokoll.MAX_EINTRAEGE + 5):
            scan_protokoll.protokolliere(db, quittung=_quittung(haendler=f"Laden {i}"),
                                         ocr_text=f"Beleg {i}", methode="ocr")
        assert len(scan_protokoll.letzte(db, grenze=1000)) == scan_protokoll.MAX_EINTRAEGE
        _leeren(db)


def test_ein_kaputter_eintrag_kippt_den_scan_nicht():
    """Das Protokoll ist ein Hilfsmittel, kein Teil des Vorgangs.

    Ein unerwarteter Wert darf nicht den ganzen Beleg kosten — der Nutzer hat
    das Foto dann schon nicht mehr.
    """
    with SessionLocal() as db:
        _leeren(db)
        scan_protokoll.protokolliere(db, quittung={"merchant": "X", "amount": "NaN", "items": None},
                                     ocr_text="egal", methode="ocr")
        eintraege = scan_protokoll.letzte(db)
        assert len(eintraege) == 1
        assert eintraege[0].betrag is None  # „NaN" ist kein Betrag
        _leeren(db)


def test_sehr_langer_rohtext_wird_gekuerzt():
    with SessionLocal() as db:
        _leeren(db)
        scan_protokoll.protokolliere(db, quittung=_quittung(),
                                     ocr_text="x" * (scan_protokoll.MAX_ZEICHEN + 500),
                                     methode="ocr")
        assert len(scan_protokoll.letzte(db)[0].ocr_text) == scan_protokoll.MAX_ZEICHEN
        _leeren(db)


def test_die_seite_zeigt_den_rohtext(logged_in_client):
    with SessionLocal() as db:
        _leeren(db)
        scan_protokoll.protokolliere(db, quittung=_quittung(haendler="Musterladen"),
                                     ocr_text="ZEILE-AUS-DEM-BELEG 9.90", methode="ocr")
    r = logged_in_client.get("/settings/scan-protokoll")
    assert r.status_code == 200
    assert "Musterladen" in r.text
    assert "ZEILE-AUS-DEM-BELEG" in r.text
    with SessionLocal() as db:
        _leeren(db)
