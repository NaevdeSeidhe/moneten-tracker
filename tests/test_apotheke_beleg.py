"""Die drei Fehler des Apotheken-Belegs .

Alle drei standen gleichzeitig in EINEM Beleg-Fenster, und jeder für sich
verdirbt die Aufstellung. Der Text hier ist der Struktur nach nachgebaut,
**alle Beträge, Namen und Nummern sind erfunden** — nur die OCR-Verstümmelungen
sind echt beobachtet.
"""

from __future__ import annotations

import pytest

from moneten.services.receipt_digital import guess_merchant
from moneten.services.receipt_split import parse_receipt_items_menge

BELEG = (
    "Apotheke Zentral\n"
    "Musterplata,1234 Musterstadt Telefon0000000000\n"
    "MUSTEBOL Kaps 250 mg 20 Stk 17.25\n"
    "Wisa FinaTta Pay 172.89\n"
    "Total 172.89\n"
)


def test_nur_die_echte_position_bleibt():
    """Drei Zeilen sahen aus wie Positionen, eine war es."""
    items = parse_receipt_items_menge(BELEG)
    assert [n for n, _p, _m in items] == ["MUSTEBOL Kaps 250 mg 20 Stk"]


@pytest.mark.parametrize("zeile", [
    "Wisa FinaTta Pay 172.89",
    "Visa Fina Pay 172.89",
    "Uisa Pay 172.89",
])
def test_die_zahlart_ist_keine_position(zeile: str):
    """OCR verwechselt V, U und W — der Filter kannte nur zwei davon.

    „Wisa FinaTta Pay" kam damit als Position durch, mit dem RECHNUNGSTOTAL als
    Preis. Die Zeile bestand aus lauter gelesenen Zeichen; nur das erste war
    falsch.
    """
    assert parse_receipt_items_menge(zeile) == []


@pytest.mark.parametrize("zeile", [
    "Musterplata,1234 Musterstadt Telefon0000000000 172.89",
    "Musterstrasse 12 172.89",
    "Musterplatz 1 172.89",
    "1234 Musterstadt 13.40",
])
def test_die_anschrift_ist_keine_position(zeile: str):
    """Die Adresse des Ladens trägt keinen Preis — sie bekam einen zugeschlagen.

    Gemessen: die Kopfzeile mit Postleitzahl, Ort und Telefonnummer stand als
    Position mit dem Beleg-Total daneben.
    """
    assert parse_receipt_items_menge(zeile) == []


def test_ein_artikel_mit_zahl_im_namen_bleibt():
    """Die Anschrift-Sperre darf keine Ware auslöschen.

    „250 mg 20 Stk" enthält Ziffern und „20 Stk" sieht einer Hausnummer
    ähnlich — trotzdem ist es ein Artikel.
    """
    items = parse_receipt_items_menge("MUSTEBOL Kaps 250 mg 20 Stk 17.25")
    assert len(items) == 1


def test_der_ort_ist_kein_haendler():
    """Der Kopf hiess „MUSTERSTADT" — der Ort aus der Adresszeile.

    Ein Ort ist kein Händler. Lieber gar kein Kopf als der falsche: der
    Händlername steuert die gelernten Regeln, und eine Regel unter „MUSTERSTADT" träfe
    jeden Beleg aus dieser Stadt.
    """
    assert guess_merchant(BELEG) == "Apotheke Zentral"


def test_ohne_erkennbaren_kopf_lieber_nichts():
    """Bleibt nach dem Filtern nichts übrig, ist ``None`` die richtige Antwort."""
    assert guess_merchant("1234 Musterstadt\nTelefon 000 000 00 00\nTotal 10.00\n") is None


@pytest.mark.parametrize("zeile", [
    "7610000000001  Gruenduengung 5.75",
    "4000000000002  Jutegarn Gruen 4.50",
])
def test_ein_barcode_ist_keine_postleitzahl(zeile: str):
    """Die Anschrift-Sperre hätte fast jeden OBI-Bon ausgelöscht.

    Ein EAN-Code endet auf vier Ziffern, und dahinter steht der Artikelname —
    genau das Muster einer Postleitzahl mit Ort. Gemessen: drei von drei
    Positionen fielen weg. Eine Postleitzahl steht ALLEIN, nicht am Ende einer
    Zahlenkette.
    """
    assert len(parse_receipt_items_menge(zeile)) == 1


@pytest.mark.parametrize("zeile", [
    "Schrauben4023 Sortiment   12.50",
    "Duebel6300 Universal      8.90",
    "Farbrolle2024 Breit      15.20",
])
def test_eine_artikelnummer_am_namen_ist_keine_anschrift(zeile: str):
    """Vier Ziffern DIREKT am Wort sind eine Artikelnummer, keine Postleitzahl.

    Die Sperre stand zuerst nur gegen benachbarte ZIFFERN — ein Buchstabe davor
    liess sie unberührt. Damit verschwand jede Position, deren Name eine
    vierstellige Nummer trägt und der ein grossgeschriebenes Wort folgt.
    Aufgefallen ist es an einem Test mit zufälligem Namen, der jedes zwanzigste
    Mal grundlos rot wurde; nachgemessen an 4000 erfundenen Namen fielen 201
    Zeilen weg.

    Eine Postleitzahl steht ALLEIN — weder Ziffer noch Buchstabe daneben.
    """
    assert len(parse_receipt_items_menge(zeile)) == 1


@pytest.mark.parametrize("zeile", [
    "WisaFinaTba Pay          17.25",   # zweiter Scan desselben Belegs, zusammengezogen
    "Wisa FinaTta Pay         17.25",   # erster Scan, mit Leerzeichen
    "VISA8.40",                          # am Betrag klebend
    "UlSA 8.40",                         # OCR liest grosses I als kleines l
    "V1SA 8.40",                         # OCR liest I als Eins
    "MaestroKarte 12.00",
    "PostFinanceCard 30.00",
    "PostFinance Card 30.00",
    "MasterCardZahlung 12.00",
    "AmexZahlung 9.00",
])
def test_die_zahlart_ist_nie_eine_position(zeile: str):
    """Die Zahlart trägt den Rechnungstotal — als Position wäre sie ein Phantomartikel.

    Der Wortgrenzen-Wächter allein reicht nicht: OCR zieht Wörter zusammen, und
    hinter „Wisa" steht dann ein Buchstabe. `(?![a-zäöü])` schlägt unter
    `IGNORECASE` auch bei einem GROSSbuchstaben zu, also griff die Regel nicht
    mehr — derselbe Beleg, zweimal gescannt, zweimal anders gelesen.

    Geprüft wird deshalb an der Zeile UND an ihrer entklebten Fassung.
    """
    assert parse_receipt_items_menge(zeile) == [], f"{zeile!r} wurde als Position gelesen"


@pytest.mark.parametrize("zeile", [
    "Nivea Visage Creme      12.90",   # „visa" steckt im Artikelnamen
    "VISAGE PFLEGE           12.90",   # derselbe Name in Versalien
    "VitaBasic Kapseln       19.80",   # Binnenversal, aber keine Marke
    "Amexander Tee            4.50",   # „amex" als Wortanfang
    "Postkarten Set           3.50",   # „post" als Wortanfang
])
def test_echte_artikel_ueberleben_den_zahlart_filter(zeile: str):
    """Die Gegenrichtung — und sie ist die gefährlichere.

    Eine zu breite Regel frisst stillschweigend echte Positionen: der Beleg geht
    dann nicht mehr auf, und im Preisverlauf fehlt ein Artikel. „Visage" ist in
    einer Apotheke keine erfundene Gefahr.
    """
    assert len(parse_receipt_items_menge(zeile)) == 1, f"{zeile!r} wurde verschluckt"


@pytest.mark.parametrize("zeile", [
    "TotalBezahlt         17.25",   # Summenzeile ohne Leerzeichen
    "RueckgeldBar          2.00",
    "BahnhofplatzZug     172.89",   # Anschrift ohne Leerzeichen
    "MwStSatz              1.20",   # MwSt traegt selbst ein Binnenversal
])
def test_auch_die_uebrigen_waechter_greifen_bei_geklebtem_text(zeile: str):
    """Dieselbe Naht-Luecke wie bei der Zahlart — an drei weiteren Filtern gemessen.

    Gefunden wurde sie nicht durch einen Bericht, sondern durch die Frage „wo
    steckt derselbe Fehler noch?". Eine Summenzeile als Position ist der
    schlimmste Fall: die Gegenprobe geht dann tautologisch auf, der Beleg gilt
    als geprüft und wird gelernt.
    """
    assert parse_receipt_items_menge(zeile) == [], f"{zeile!r} wurde als Position gelesen"


@pytest.mark.parametrize("zeile", [
    "TotalCare Zahnpasta   4.50",   # „Total" steckt im Produktnamen
    "Totalflex Binde       6.80",
    "Wegwerfrasierer       3.90",   # „weg" als Wortanfang
    "Barbecue Sauce        4.20",   # „bar" als Wortanfang
    "Satz Schrauben       12.00",
    "Milchflasche          9.90",   # enthaelt „chf"
])
def test_die_strengere_summenregel_frisst_keine_artikel(zeile: str):
    """`_SKIP` auf die entklebte Zeile loszulassen waere hier falsch gewesen.

    Dort stehen breite Woerter wie „total", „karte", „preis". Uebersprungen wird
    deshalb nur, wenn JEDES Wort der Zeile Bon-Vokabular ist — ein einziges
    unbekanntes, und sei es „Care", laesst die Zeile durch.
    """
    assert len(parse_receipt_items_menge(zeile)) == 1, f"{zeile!r} wurde verschluckt"
