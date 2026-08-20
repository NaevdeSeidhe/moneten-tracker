"""Die Hausrat-/Privathaftpflicht-Police als Verlaufswert.

Die Reihe blieb leer, obwohl der Beleg im Ordner lag: sein Dateiname enthält
„Versicherungspolice", die Zuordnung schickte ihn an den KRANKENkassen-Parser,
und der fand keine KVG-Zeile und gab stillschweigend nichts zurück.

**Der Text hier ist nachgebaut, alle Beträge erfunden** — die Struktur stammt
vom echten Beleg, die Zahlen nicht. Nachgebildet ist auch der OCR-Schaden: die
Police wird als Bild gescannt, und die Erkennung verliert die Umlaute
zuverlässig („Pramienubersicht", „Jahrespramie").
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from moneten.services.belege_parser import (
    PARSER,
    PruefsummeFehler,
    hausrat_police,
    police,
)


def _police(hausrat="200.00", haftpflicht="100.00", stempel="15.00",
            versicherung="300.00", jahr="315.00", beginn="16.07.2024") -> str:
    """Eine Police in der Form, in der OCR sie liefert — ohne Umlaute."""
    return (
        "Die Mobilverkehr Versicherung\n"
        "Hausrat- und Privathaftpflichtversicherung\n"
        f"Vertragsbeginn:{beginn}\n"
        "Vertragsablauf:30.06.2034\n"
        "Pramienubersicht\n"
        f"Hausratversicherung-1234Musterstadt,Musterweg1 CHF {hausrat}\n"
        f"Privathaftpflichtversicherung CHF {haftpflicht}\n"
        f"Versicherungspramie CHF {versicherung}\n"
        f"Stempelabgabe CHF {stempel}\n"
        f"Jahrespramie CHF {jahr}\n"
        "DiePramieistjahrlichzahlbar.Falligkeit:01.08.\n"
    )


def test_jahrespraemie_wird_gelesen():
    """Der Wert ist die Jahresprämie INKLUSIVE Stempelabgabe.

    Das ist der Betrag, der vom Konto geht — und damit der einzige, der sich
    gegen die Buchung abgleichen lässt.
    """
    befunde = hausrat_police(_police())
    assert len(befunde) == 1
    assert befunde[0].slug == "hausrat_haftpflicht"
    assert befunde[0].value == Decimal("315.00")


def test_periode_ist_das_kalenderjahr():
    """Nicht das Versicherungsjahr ab dem Beginndatum.

    Die Reihe ist jährlich; ein Punkt vom 16.07. bis zum 15.07. liesse sich mit
    keinem anderen Jahreswert vergleichen.
    """
    b = hausrat_police(_police(beginn="16.07.2024"))[0]
    assert b.period_start == date(2024, 1, 1)
    assert b.period_end == date(2024, 12, 31)


def test_die_aufteilung_reist_als_positionen_mit():
    """Ohne sie wäre eine gestiegene Prämie nicht als „mehr Deckung" lesbar."""
    b = hausrat_police(_police())[0]
    posten = {k[len("pos:"):]: Decimal(v) for k, v in b.extras.items() if k.startswith("pos:")}
    assert posten == {
        "Hausrat": Decimal("200.00"),
        "Privathaftpflicht": Decimal("100.00"),
        "Stempelabgabe": Decimal("15.00"),
    }
    assert sum(posten.values()) == b.value


def test_falsch_gelesene_position_faellt_auf():
    """Die Police hat eine eigene Prüfsumme — und die wird auch benutzt.

    Eine Zahl, die niemand nachrechnet, ist gefährlicher als eine fehlende:
    sie steht im Diagramm und sieht aus wie ein gelesener Wert.
    """
    with pytest.raises(PruefsummeFehler):
        hausrat_police(_police(hausrat="900.00"))


def test_falsche_zwischensumme_faellt_auf():
    """Auch die ausgewiesene Versicherungsprämie wird gegengerechnet."""
    with pytest.raises(PruefsummeFehler):
        hausrat_police(_police(versicherung="450.00"))


def test_ohne_jahrespraemie_kein_befund():
    """Ein Dokument ohne diese Zeile ist keine Police — und keine Vermutung wert."""
    assert hausrat_police("Irgendein Brief der Versicherung\nMit freundlichen Gruessen\n") == []


def test_der_krankenkassen_parser_bleibt_unberuehrt():
    """Er darf diese Police weiterhin nicht lesen — sonst wäre nichts gewonnen.

    Genau das war der Fehler: beide Dokumente tragen „Police" im Namen, und der
    KVG-Parser bekam ein Dokument, in dem es keine KVG-Zeile gibt.
    """
    assert police(_police()) == []


def test_der_parser_ist_registriert():
    """Ohne Eintrag in ``PARSER`` findet das Extraktionsskript ihn nicht."""
    assert PARSER["hausrat"] is hausrat_police


def test_die_nachbarspalte_wird_nicht_mitgelesen():
    """Gemessen an einer Police — die Beträge hier sind erfunden, der Fehler war
    echt: aus „40.00" wurde „40.0012345", weil die Nachbarspalte hinten anklebte.

    Das Betragsmuster stand auf „Ziffern, Trenner und Leerzeichen, beliebig
    lang" und las damit ueber das Leerzeichen hinweg in die naechste Spalte
    weiter. Der WERT stimmte trotzdem, weil die Summe aufging — in der
    Aufstellung stand danach eine Zahl mit sieben Nachkommastellen. Ein Betrag
    endet nach den Rappen.
    """
    kaputt = ("Privathaftpflichtversicherung\n"
              "Vertragsbeginn:01.01.2020\n"
              "Privathaftpflichtversicherung CHF 40.00 12345\n"
              "Stempelabgabe CHF 2.00\n"
              "Jahrespramie CHF 42.00\n")
    b = hausrat_police(kaputt)[0]
    assert b.extras["pos:Privathaftpflicht"] == "40.00"
    assert b.value == Decimal("42.00")


def test_pruefsumme_greift_schon_ab_zwei_posten():
    """Eine reine Privathaftpflicht-Police traegt nur zwei Zeilen.

    Die Pruefung lief vorher erst ab drei Posten — und genau diese Police lief
    damit ungeprueft durch.
    """
    # Absichtlich nicht aufgehend: 61.80 + 9.09 ist nicht 42.00 — genau daran
    # muss die Prüfsumme anschlagen.
    zwei = ("Privathaftpflichtversicherung\n"
            "Vertragsbeginn:01.01.2020\n"
            "Privathaftpflichtversicherung CHF 61.80\n"
            "Stempelabgabe CHF 9.09\n"
            "Jahrespramie CHF 42.00\n")
    with pytest.raises(PruefsummeFehler):
        hausrat_police(zwei)
