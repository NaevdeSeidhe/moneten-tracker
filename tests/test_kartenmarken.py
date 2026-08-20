"""Zahlarten sind keine Artikel — echte Artikel aber auch keine Zahlarten.

Im Preisverlauf stand ein Eintrag „Uisa" mit schwankendem Preis. Das war die
Kartenmarke VISA, vom Beleg-Scan als Position gelesen, mit dem Rechnungstotal
als „Preis" — und weil das Total je Beleg anders ist, sah es nach einem
Preisverlauf aus.

Nachgemessen fielen genau diese Schreibweisen durch den alten Filter:

    ULSA / VLSA / V1SA / UlSA    OCR verwechselt I, l und 1 routinemässig;
                                 „UlSA" sieht serifenlos identisch aus wie „Uisa"
    VISA8.40                     am Betrag klebend, dadurch griff ``\\b`` nicht
    Mastercard                   stand überhaupt nicht in der Liste

Die zweite Hälfte dieser Datei ist genauso wichtig: Der erste Anlauf des Filters
verschluckte „Visanella Käse" — einen echten Artikel, in dem zufällig „visa"
steckt. Ein Filter, der Positionen frisst, ist schlimmer als der Fehler, den er
beheben soll.
"""

from __future__ import annotations

import pytest

from moneten.services.price_history import ist_artikel
from moneten.services.receipt_split import parse_receipt_items

ZAHLARTEN = [
    "VISA 8.40", "UISA 8.40", "Uisa 8.40",
    "UlSA 8.40",      # grosses I als kleines L gelesen
    "ULSA 8.40", "VLSA 8.40",
    "V1SA 8.40",      # als Eins gelesen
    "VISA8.40", "UISA8.40",   # am Betrag klebend
    "MAESTRO 8.40", "Mastercard 8.40", "MASTER CARD 8.40",
    "PostFinance Card 8.40", "TWINT 8.40",
]

ECHTE_ARTIKEL = [
    ("Visanella Kaese 4.20", "Visanella Kaese"),
    ("Maestrale Pasta 2.80", "Maestrale Pasta"),
    ("Amexo Reiniger 3.50", "Amexo Reiniger"),
    ("Visagismus Set 9.90", "Visagismus Set"),
    ("Butter Bio 250g 5.90", "Butter Bio 250g"),
]


@pytest.mark.parametrize("zeile", ZAHLARTEN)
def test_zahlart_wird_keine_position(zeile: str) -> None:
    assert parse_receipt_items(zeile) == [], f"{zeile!r} wurde als Artikel gelesen"


@pytest.mark.parametrize(("zeile", "erwartet"), ECHTE_ARTIKEL)
def test_echter_artikel_ueberlebt_den_filter(zeile: str, erwartet: str) -> None:
    """Gegenprobe — sonst wäre der Filter zu gierig und niemand merkte es."""
    treffer = parse_receipt_items(zeile)
    assert treffer, f"{zeile!r} wurde faelschlich gefiltert"
    assert treffer[0][0] == erwartet


@pytest.mark.parametrize("name", ["Uisa", "UlSA", "VISA", "Mastercard", "TWINT", "Total"])
def test_gespeicherte_zahlart_faellt_aus_dem_preisverlauf(name: str) -> None:
    """Zweiter Filter beim Auswerten — für Belege, die schon gescannt sind.

    Ohne ihn müsste man alle Quittungen neu einlesen, nur um einen falschen
    Eintrag loszuwerden.
    """
    assert ist_artikel(name) is False


@pytest.mark.parametrize("name", ["Visanella Kaese", "Maestrale Pasta", "Butter Bio 250g",
                                  "Totalisator Zeitung"])
def test_echter_artikel_bleibt_im_preisverlauf(name: str) -> None:
    assert ist_artikel(name) is True
