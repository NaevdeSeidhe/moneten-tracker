"""Zahlen, die keine Beträge sind — und die der Parser trotzdem als solche las.

Gemessen an einem Apotheken-Beleg: von fünf gelesenen „Beträgen" war keiner
einer. Die grösste dieser Zahlen — eine Bon-Nummer — wurde zum Beleg-Total, und
zwei Kopfzeilen wurden zur Position.

**Der Text ist im Aufbau nachgebaut, alle Zahlen und Namen sind erfunden.** Echt
sind nur die OCR-Verstümmelungen: Ziffern, die aneinanderkleben, und ein Punkt,
den die Erkennung in eine Nummer hineinliest.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from moneten.services.receipt_digital import guess_merchant
from moneten.services.receipt_ocr import extract_amount, extract_date
from moneten.services.receipt_split import parse_receipt_items_menge

# Aufbau eines Apotheken-Bons, wie OCR ihn liefert.
BELEG = """MUSTERSTADT
APOTHEKE
Marktgasse,1234 Musterstadt
Telefon0000000000
09.03.202611:2233445566 Kasse 2/17
Sie wurden beraten von Frau A.Muster
884.551
VITAMINPRAEPARAT Kaps 100 mg 30 Stk
1* 24.60 24.603
Total inkl. MWSr T24.60
Wisa FinaTta Pay 24.60
Ink.1usive MWST 8.1% 0.00
MWS:T-Num:CHE-123.456.789 MWST
33440711223344
"""


def test_nur_die_echte_position_bleibt():
    """Eine Ware auf dem Beleg, eine Position im Ergebnis."""
    items = parse_receipt_items_menge(BELEG)
    assert [n for n, _p, _m in items] == ["VITAMINPRAEPARAT Kaps 100 mg 30 Stk"]
    assert items[0][1] == Decimal("24.60")


def test_die_bon_nummer_wird_nicht_zum_total():
    """Der schwerste der fünf Fehler.

    OCR liest die Bon-Nummer „884551" als „884.551". Das Betragsmuster
    akzeptierte davon „884.55" — und weil ``extract_amount`` ohne erkanntes
    Total den GRÖSSTEN Betrag nimmt, wurde die Nummer zum Beleg-Total. Ein
    Betrag hat genau zwei Nachkommastellen.
    """
    assert extract_amount(BELEG) == Decimal("24.60")


@pytest.mark.parametrize(("text", "erwartet"), [
    ("884.551", None),          # Bon-Nummer
    ("24.603", None),           # Betrag mit angeklebtem MwSt-Schlüssel
    ("CHE-123.456.789", None),  # MwSt-Nummer
    ("24.60", Decimal("24.60")),
    ("1'234.50", Decimal("1234.50")),
])
def test_genau_zwei_nachkommastellen(text: str, erwartet):
    """Drei Nachkommastellen sind kein Betrag — egal wie plausibel sie aussehen."""
    assert extract_amount(text) == erwartet


def test_ein_datum_ist_kein_betrag():
    """„09.03.2026" wurde als Betrag 09.03 gelesen."""
    assert extract_amount("09.03.2026 Kasse 2/17") is None


def test_datum_wird_gefunden_obwohl_es_klebt():
    """Datum, Uhrzeit und Bon-Nummer bilden EINE Zeichenkette.

    Kein Muster fand darin das Datum, und der Beleg landete ohne Datum im
    Editor — von Hand nachzutragen, bei jedem Beleg dieses Ladens.
    """
    assert extract_date(BELEG) == date(2026, 3, 9)


def test_der_beratungssatz_wird_keine_position():
    """„Sie wurden beraten von …" ist ein Satz, keine Ware.

    Ohne Sperre stiftete er den Namen für die nächste Zeile mit einem Betrag.
    """
    items = parse_receipt_items_menge("Sie wurden beraten von Frau A.Muster\n884.55\n")
    assert items == []


def test_kassenzeile_wird_keine_position():
    assert parse_receipt_items_menge("Kasse 2/17 24.60") == []


def test_haendlername_ueber_zwei_zeilen():
    """Ein umgebrochenes Logo ist EIN Name.

    Der Kopf hiess vorher „MUSTERSTADT" — also der Ort. Ein Ort als Händler ist
    doppelt schlecht: er steht im Beleg, und er steuert die gelernten Regeln.
    """
    assert guess_merchant(BELEG) == "MUSTERSTADT APOTHEKE"


def test_lange_kopfzeile_bleibt_allein():
    """Zusammengezogen wird nur, wenn die erste Zeile allein zu kurz ist.

    Sonst wüchse jeder Händlername um seine Adresszeile.
    """
    assert guess_merchant("Grossverteiler Musterstadt AG\nMarktgasse 4\n") \
        == "Grossverteiler Musterstadt AG"
