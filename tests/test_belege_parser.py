"""Belegtexte deuten: Prämie, Strom, Vorsorge, Police, Verbilligung, Steuern.

**Sämtliche Texte, Beträge und Namen in dieser Datei sind erfunden.** Aus dem
Bestand stammt nur das *Layout* — die Reihenfolge der Felder, die
Zeilenumbrüche mitten im Wort, die Schreibweise der Zahlen. Genau das ist der
Grund, warum ``belege_parser`` keine Datei liest und kein PDF kennt: so lässt
sich jede Regel prüfen, ohne dass je ein echter Beleg in die Tests gerät.

Die Zahlen sind bewusst so gewählt, dass sie keiner realen Abrechnung ähneln
(80 Rappen je kWh, 88'888 Franken Jahreslohn) und sich im Kopf nachrechnen
lassen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from moneten.services.belege_parser import (
    _MINDESTWERT,
    PARSER,
    betrag,
    datum,
    flach,
    monate_zwischen,
    plausibel,
    police,
    praemienabrechnung,
    praemienverbilligung,
    steuerrechnung,
    stromrechnung,
    vorsorgeausweis,
)

# ---------------------------------------------------------------- Bausteine


def test_hochkomma_ist_kein_dezimaltrenner() -> None:
    assert betrag("1'234.50") == Decimal("1234.50")


def test_akut_zeichen_trennt_wie_ein_hochkomma() -> None:
    """Der Lohnausweis setzt ´ statt ' — dieselbe Zahl, anderes Zeichen."""
    assert betrag("1´234.50") == Decimal("1234.50")


def test_typografischer_apostroph_trennt_wie_ein_hochkomma() -> None:
    """Manche Scans liefern ’ — ohne diesen Fall bliebe der Betrag ungelesen."""
    assert betrag("1’234.50") == Decimal("1234.50")


def test_leerzeichen_als_tausendertrenner() -> None:
    assert betrag("12 345.00") == Decimal("12345.00")


def test_abschliessender_strich_meint_null_rappen() -> None:
    assert betrag("450.-") == Decimal("450")


def test_kein_betrag_gibt_none_statt_zu_werfen() -> None:
    """Belegtexte enthalten Zahlen, die keine Beträge sind — der Aufrufer entscheidet."""
    assert betrag("Referenznummer") is None
    assert betrag("") is None
    assert betrag("   ") is None


def test_datum_vierstellig_und_zweistellig() -> None:
    assert datum("31.12.2025") == date(2025, 12, 31)
    assert datum("30.06.26") == date(2026, 6, 30)


def test_zweistelliges_jahr_ueber_der_grenze_ist_das_letzte_jahrhundert() -> None:
    """Ein „85" kann nur ein Lesefehler sein — und soll dann auch alt aussehen."""
    assert datum("30.06.85") == date(1985, 6, 30)


def test_unmoegliches_datum_gibt_none() -> None:
    assert datum("31.02.2025") is None


def test_datum_muss_die_ganze_zeichenkette_sein() -> None:
    """``fullmatch``: „Bezahlt " ist kein Datumsfeld, sondern Fliesstext."""
    assert datum("Bezahlt ") is None
    assert datum("2025-06-30") is None


def test_monate_zwischen_umfasst_beide_raender() -> None:
    assert monate_zwischen(date(2020, 3, 5), date(2020, 3, 20)) == [date(2020, 3, 1)]


def test_monate_zwischen_ueber_den_jahreswechsel() -> None:
    """Der Dezember-Sprung ist die einzige Stelle, an der die Jahreszahl wächst."""
    assert monate_zwischen(date(2019, 11, 15), date(2020, 2, 3)) == [
        date(2019, 11, 1), date(2019, 12, 1), date(2020, 1, 1), date(2020, 2, 1),
    ]


def test_flach_macht_umbrueche_und_mehrfach_leerzeichen_zu_einem_leerzeichen() -> None:
    """Die Textebene bricht Wörter am Zeilenende — „Prämi\\nenverbilligung"."""
    assert flach("  Prämi\nenverbilligung   in    der\n\n Höhe  ") == (
        "Prämi enverbilligung in der Höhe"
    )


def test_kleinstwert_ist_nicht_plausibel() -> None:
    """„CHF 4" Prämienverbilligung kam aus einem Lesefehler, nicht aus der Verfügung."""
    assert plausibel("kk_verbilligung", Decimal("4")) is False
    assert plausibel("kk_praemie", Decimal("4")) is False
    assert plausibel("lohn", Decimal("4999")) is False


def test_die_untergrenze_selbst_gilt_noch_als_plausibel() -> None:
    assert plausibel("kk_praemie", Decimal("50")) is True
    assert plausibel("kk_verbilligung", Decimal("200")) is True


def test_reihe_ohne_untergrenze_laesst_alles_durch() -> None:
    """Ohne Eintrag gibt es keine Meinung — nicht stillschweigend alles verwerfen."""
    assert plausibel("gibt_es_nicht", Decimal("0.05")) is True
    assert plausibel("gibt_es_nicht", Decimal("0")) is True


def test_null_faellt_ohne_bestaetigung_durch() -> None:
    """Auf jedem Beleg stehen mehrere Nullen — eine gegriffene sagt nichts aus."""
    assert plausibel("steuern_kanton", Decimal("0")) is False
    assert plausibel("steuern_bund", Decimal("0")) is False


def test_bestaetigte_null_kommt_durch() -> None:
    """„Keine Steuer geschuldet" ist eine Beobachtung, keine Lücke."""
    assert plausibel("steuern_kanton", Decimal("0"), null_bestaetigt=True) is True


def test_die_bestaetigung_gilt_nur_fuer_die_null() -> None:
    """Sonst wäre sie ein Schalter zum Abstellen des Schutzes.

    Ein Wert knapp über null bleibt verdächtig: so sieht eine gegriffene
    Teilposition aus, nicht eine Steuerforderung.
    """
    assert plausibel("steuern_kanton", Decimal("7"), null_bestaetigt=True) is False


# ---------------------------------------------------------------- Prämienabrechnung

_ABRECHNUNG_MEHRERE_MONATE = """
MUSTERKASSE Gesundheitsorganisation
Prämienabrechnung

Versicherte Person       Test Muster
Police-Nr.               ZZZ 000 000

Zeitraum                 Monatsprämie CHF
01.02.2020 - 30.06.2020
                         333.55

Total                  1'667.75
"""

_ABRECHNUNG_EIN_MONAT = """
MUSTERKASSE Gesundheitsorganisation
Prämienabrechnung

01.09.2021 - 30.09.2021        288.80
"""


def test_abrechnung_ueber_mehrere_monate_liefert_je_monat_einen_befund() -> None:
    """Sonst stünde für vier von fünf Monaten nichts da, obwohl der Beleg sie ausweist."""
    befunde = praemienabrechnung(_ABRECHNUNG_MEHRERE_MONATE)
    assert [b.period_start for b in befunde] == [
        date(2020, 2, 1), date(2020, 3, 1), date(2020, 4, 1),
        date(2020, 5, 1), date(2020, 6, 1),
    ]
    assert {b.slug for b in befunde} == {"kk_praemie"}


def test_alle_monate_einer_abrechnung_tragen_dieselbe_monatspraemie() -> None:
    """Die Gesamtsumme (1'667.75) darf nicht in einem einzelnen Monat landen."""
    werte = {b.value for b in praemienabrechnung(_ABRECHNUNG_MEHRERE_MONATE)}
    assert werte == {Decimal("333.55")}


def test_periodenende_ist_der_monatsletzte_auch_im_schaltjahr() -> None:
    """Februar 2020 hat 29 Tage — ein festes „28" fiele hier auf."""
    enden = [b.period_end for b in praemienabrechnung(_ABRECHNUNG_MEHRERE_MONATE)]
    assert enden == [
        date(2020, 2, 29), date(2020, 3, 31), date(2020, 4, 30),
        date(2020, 5, 31), date(2020, 6, 30),
    ]


def test_abrechnung_ueber_einen_monat_liefert_genau_einen_befund() -> None:
    befunde = praemienabrechnung(_ABRECHNUNG_EIN_MONAT)
    assert len(befunde) == 1
    assert befunde[0].value == Decimal("288.80")
    assert befunde[0].period_start == date(2021, 9, 1)
    assert befunde[0].period_end == date(2021, 9, 30)


def test_verdrehte_periode_liefert_nichts() -> None:
    """Ende vor Beginn heisst verlesen — dann lieber kein Wert.

    Bewusst innerhalb *eines* Monats verdreht: über Monatsgrenzen hinweg fiele
    die Rückwärts-Periode ohnehin durch, weil die Monatsschleife gar nicht erst
    anläuft. Nur so misst der Test die Prüfung und nicht ihren Nebeneffekt.
    """
    assert praemienabrechnung("30.06.2020 - 01.06.2020   333.55") == []


def test_praemie_null_liefert_nichts() -> None:
    assert praemienabrechnung("01.03.2020 - 31.03.2020   0.00") == []


def test_abrechnung_ohne_periode_liefert_nichts() -> None:
    assert praemienabrechnung("Prämienabrechnung\nBetrag 333.55") == []


# ---------------------------------------------------------------- Stromrechnung

_STROM_QUARTAL = """
Stromwerk AG
Quartalsrechnung Nr. 900123

Zeitraum vom 1. Oktober 2024 bis 31. Dezember 2024

Energielieferung
Wirkenergie 01.10.24 - 31.12.24 (92 Tage)     800 kWh
Netznutzung
Wirkenergie 01.10.24 - 31.12.24 (92 Tage)     800 kWh

Rechnungsbetrag      640.00
"""

_STROM_JAHR = """
Stromwerk AG
Jahresrechnung 2024

Zeitraum vom 1. Januar 2024 bis 31. Dezember 2024

Energielieferung
Wirkenergie 01.01.24 - 31.03.24 (91 Tage)     700 kWh

Total Energielieferung, Netznutzung und Abgaben    2'480.00
Abzüglich Akontozahlungen                         -1'900.00
Rechnungsbetrag                                      580.00
"""

_STROM_AKONTO = """
Stromwerk AG
Akontorechnung 2. Quartal 2025

Zeitraum vom 1. April 2025 bis 30. Juni 2025

Wirkenergie 01.04.25 - 30.06.25 (91 Tage)     500 kWh

Rechnungsbetrag      410.00
"""

_STROM_OHNE_ART = """
Stromwerk AG
Rechnung Nr. 900456

Zeitraum vom 1. Juli 2025 bis 30. September 2025

Rechnungsbetrag      222.00
"""


def test_akontorechnung_liefert_keinen_befund() -> None:
    """Eine Vorauszahlung ist keine Kostenposition.

    Der Text trägt Periode und Rechnungsbetrag — ohne die Fallunterscheidung
    entstünde hier ein Befund, und für 2025 stünden die Vorauszahlungen *und*
    die Jahresabrechnung in derselben Kurve.
    """
    assert stromrechnung(_STROM_AKONTO) == []


def test_jahresrechnung_nimmt_den_betrag_vor_abzug_der_akontos() -> None:
    """Der ausgewiesene „Rechnungsbetrag" (580.00) ist nur die Restzahlung."""
    befunde = stromrechnung(_STROM_JAHR)
    assert len(befunde) == 1
    assert befunde[0].value == Decimal("2480.00")
    assert befunde[0].period_start == date(2024, 1, 1)
    assert befunde[0].period_end == date(2024, 12, 31)
    assert befunde[0].extras["rechnungsart"] == "Jahresrechnung"


def test_jahresrechnung_meldet_keinen_verbrauch() -> None:
    """Die erste Wirkenergie-Zeile deckt nur ein Quartal ab.

    Mit dem Jahresbetrag gepaart ergäbe sie einen etwa vierfach zu hohen
    Rappenpreis — lieber kein Verbrauch als ein falscher.
    """
    extras = stromrechnung(_STROM_JAHR)[0].extras
    assert "kwh" not in extras
    assert "rp_kwh" not in extras


def test_quartalsrechnung_nimmt_den_rechnungsbetrag() -> None:
    befunde = stromrechnung(_STROM_QUARTAL)
    assert len(befunde) == 1
    assert befunde[0].slug == "strom"
    assert befunde[0].value == Decimal("640.00")
    assert befunde[0].period_start == date(2024, 10, 1)
    assert befunde[0].period_end == date(2024, 12, 31)


def test_quartalsrechnung_meldet_verbrauch_und_rappenpreis() -> None:
    """640.00 auf 800 kWh sind 80 Rappen — die Grösse, die Tarifsprünge zeigt."""
    extras = stromrechnung(_STROM_QUARTAL)[0].extras
    assert extras["kwh"] == "800"
    assert extras["rp_kwh"] == "80.00"
    assert extras["rechnungsart"] == "Quartalsrechnung"


def test_rechnung_ohne_art_gilt_als_quartalsrechnung() -> None:
    """Der häufigste Fall ist die Vorgabe — eine Rechnung ohne Kennwort ist keine Akonto."""
    befunde = stromrechnung(_STROM_OHNE_ART)
    assert len(befunde) == 1
    assert befunde[0].value == Decimal("222.00")
    assert befunde[0].extras["rechnungsart"] == "Quartalsrechnung"
    assert "kwh" not in befunde[0].extras


def test_stromrechnung_ohne_zeitraum_liefert_nichts() -> None:
    assert stromrechnung("Stromwerk AG\nQuartalsrechnung\nRechnungsbetrag 222.00") == []


def test_stromrechnung_ohne_betrag_liefert_nichts() -> None:
    assert stromrechnung(
        "Stromwerk AG\nQuartalsrechnung\nZeitraum vom 1. Juli 2025 bis 30. September 2025"
    ) == []


# ---------------------------------------------------------------- Vorsorgeausweis

_VORSORGEAUSWEIS = """
Pensionskasse Musterstiftung
Vorsorgeausweis per 01.07.2026

Versicherte Person          Test Muster
Beschäftigungsgrad          80.0 %
AHV-Jahreslohn                          88'888.00
Versicherter Jahreslohn 1               66'666.00

                                   BVG-Anteil        Total
Altersguthaben am 01.01.2026         11'111.10    22'222.20
Altersguthaben am 01.07.2026         33'333.30    77'777.70

                            Arbeitnehmer  Arbeitgeber    Total
Total Monatsbeitrag               123.45       234.56   358.01
"""


def test_vorsorgeausweis_liefert_guthaben_und_lohn_getrennt() -> None:
    """Ein Bestand und eine Jahresgrösse — nicht in dieselbe Reihe werfen."""
    befunde = vorsorgeausweis(_VORSORGEAUSWEIS)
    assert {b.slug for b in befunde} == {"pk_guthaben", "lohn"}


def test_guthaben_ist_der_stand_zum_stichtag_nicht_zum_jahresanfang() -> None:
    """Beide Zeilen heissen „Altersguthaben am" — die erste ist die falsche."""
    guthaben = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "pk_guthaben")
    assert guthaben.value == Decimal("77777.70")
    assert guthaben.period_start == date(2026, 7, 1)
    assert guthaben.period_end == date(2026, 7, 1)


def test_guthaben_ist_das_total_nicht_der_bvg_anteil() -> None:
    """Der BVG-Anteil (33'333.30) ist ein Teil des Guthabens, nicht das Guthaben."""
    guthaben = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "pk_guthaben")
    assert guthaben.value != Decimal("33333.30")


def test_monatsbeitrag_landet_als_nebenwert() -> None:
    guthaben = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "pk_guthaben")
    assert guthaben.extras["beitrag_an"] == "123.45"
    assert guthaben.extras["beitrag_ag"] == "234.56"
    assert guthaben.extras["beitrag_monat"] == "358.01"


def test_lohn_gilt_fuer_das_kalenderjahr_des_stichtags() -> None:
    lohn = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "lohn")
    assert lohn.value == Decimal("88888.00")
    assert lohn.period_start == date(2026, 1, 1)
    assert lohn.period_end == date(2026, 12, 31)


def test_lohn_traegt_versicherten_lohn_und_pensum_als_nebenwerte() -> None:
    """Die Fussnotenziffer hinter „Versicherter Jahreslohn" gehört nicht zum Betrag."""
    lohn = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "lohn")
    assert lohn.extras["versicherter_lohn"] == "66666.00"
    assert lohn.extras["pensum"] == "80.0"


def test_lohn_aus_dem_ausweis_traegt_einen_vorbehalt() -> None:
    """Er ist hochgerechnet — bei Pensumwechsel mitten im Jahr stimmt er nicht."""
    lohn = next(b for b in vorsorgeausweis(_VORSORGEAUSWEIS) if b.slug == "lohn")
    assert lohn.hinweis


def test_vorsorgeausweis_ohne_stichtag_liefert_nichts() -> None:
    assert vorsorgeausweis("Pensionskasse\nAltersguthaben  1'000.00 2'000.00") == []


# ---------------------------------------------------------------- Police

_POLICE_ALT = """
MUSTERKASSE Gesundheitsorganisation
Versicherungspolice

Gültig ab: 01.01.2021

Obligatorische Krankenpflegeversicherung MODELL BASIS      444.40
Zusatzversicherung ZUSATZ PLUS                                     99.90

Die Jahresfranchise beträgt CHF 1'750.00
"""

_POLICE_NEU = """
MUSTERKASSE GESUNDHEITSORGANISATION
POLICE

GÜLTIG AB 01.01.2025

Obligatorische Krankenpflegeversicherung MODELL BASIS      488.80
"""

_POLICE_OHNE_MODELL = """
MUSTERKASSE
Gültig ab: 01.01.2019

Obligatorische Krankenpflegeversicherung      555.50
"""


def test_police_liest_die_grundversicherung_nicht_die_gesamtpraemie() -> None:
    """Nur die KVG-Prämie ist über die Jahre vergleichbar; Zusätze kommen und gehen."""
    befunde = police(_POLICE_ALT)
    assert len(befunde) == 1
    assert befunde[0].slug == "kk_police"
    assert befunde[0].value == Decimal("444.40")


def test_police_gilt_fuer_das_kalenderjahr_ab_gueltigkeit() -> None:
    befund = police(_POLICE_ALT)[0]
    assert befund.period_start == date(2021, 1, 1)
    assert befund.period_end == date(2021, 12, 31)


def test_modellname_landet_in_den_extras() -> None:
    """Ein Modellwechsel erklärt Sprünge, die sonst wie eine Preiserhöhung aussähen."""
    assert police(_POLICE_ALT)[0].extras["modell"] == "MODELL BASIS"
    assert police(_POLICE_NEU)[0].extras["modell"] == "MODELL BASIS"


def test_altes_layout_mit_doppelpunkt_wird_gelesen() -> None:
    """„Gültig ab: 01.01.2021" — ohne das optionale ``:`` fänden sich nur neue Policen."""
    assert police(_POLICE_ALT)[0].period_start == date(2021, 1, 1)


def test_neues_layout_ohne_doppelpunkt_wird_gelesen() -> None:
    """„GÜLTIG AB 01.01.2025" — Grossschreibung und kein Doppelpunkt."""
    befunde = police(_POLICE_NEU)
    assert len(befunde) == 1
    assert befunde[0].period_start == date(2025, 1, 1)
    assert befunde[0].value == Decimal("488.80")


def test_franchise_landet_in_den_extras() -> None:
    assert police(_POLICE_ALT)[0].extras["franchise"] == "1750.00"


def test_police_ohne_modellnamen_bleibt_lesbar() -> None:
    """Der Modellname ist ein Zusatz, keine Bedingung."""
    befunde = police(_POLICE_OHNE_MODELL)
    assert len(befunde) == 1
    assert befunde[0].value == Decimal("555.50")
    assert "modell" not in befunde[0].extras


def test_police_ohne_gueltigkeitsdatum_liefert_nichts() -> None:
    assert police("MUSTERKASSE\nObligatorische Krankenpflegeversicherung MODELL BASIS 488.80") == []


# ---------------------------------------------------------------- Prämienverbilligung

_VERBILLIGUNG = """
Kanton Muster
Amt für Gesundheit

Verfügung für das Jahr 2022

Sie erhalten für das Jahr 2022 eine Prämi
enverbilligung in der Höhe von CHF 1'234.00.
"""


def test_verbilligung_gilt_fuers_ganze_jahr() -> None:
    befunde = praemienverbilligung(_VERBILLIGUNG)
    assert len(befunde) == 1
    assert befunde[0].slug == "kk_verbilligung"
    assert befunde[0].value == Decimal("1234.00")
    assert befunde[0].period_start == date(2022, 1, 1)
    assert befunde[0].period_end == date(2022, 12, 31)


def test_verbilligung_gilt_als_unsicher() -> None:
    """OCR-Quelle: ein verschobenes Komma wäre hier teuer und lautlos."""
    befund = praemienverbilligung(_VERBILLIGUNG)[0]
    assert befund.unsicher is True
    assert befund.hinweis


def test_verbilligung_ohne_jahr_liefert_nichts() -> None:
    assert praemienverbilligung("Verbilligung in der Höhe von CHF 1'234.00") == []


# ---------------------------------------------------------------- Steuern

_STEUER_KANTON = """
Steuerverwaltung Kanton Muster
Ordentiiche Steuern

Kantons- und Gemeindesteuern 2019
Test Muster, Musterweg 1

Rechnungsbetrag CHF 3'210.00
"""

_STEUER_BUND = """
Eidgenössische Steuerverwaltung

Direkte Bundessteuer 2019
Test Muster, Musterweg 1

Steuerbetrag CHF 456.00
"""


def test_kantonssteuer_landet_in_der_kantonsreihe() -> None:
    befunde = steuerrechnung(_STEUER_KANTON)
    assert len(befunde) == 1
    assert befunde[0].slug == "steuern_kanton"
    assert befunde[0].value == Decimal("3210.00")
    assert befunde[0].period_start == date(2019, 1, 1)
    assert befunde[0].period_end == date(2019, 12, 31)


def test_bundessteuer_landet_in_der_bundesreihe() -> None:
    """Beide Rechnungen sehen fast gleich aus — nur das Wort „Bundessteuer" trennt sie."""
    befunde = steuerrechnung(_STEUER_BUND)
    assert len(befunde) == 1
    assert befunde[0].slug == "steuern_bund"
    assert befunde[0].value == Decimal("456.00")


def test_steuerbefund_gilt_als_unsicher() -> None:
    """Die schwächste Quelle im Bestand — Zeichenfehler bis in die Überschriften."""
    befund = steuerrechnung(_STEUER_KANTON)[0]
    assert befund.unsicher is True
    assert befund.hinweis


def test_steuerrechnung_ohne_betrag_liefert_nichts() -> None:
    """Nur mit Jahr *und* Betrag — ein halber Befund wäre schlimmer als keiner."""
    assert steuerrechnung("Kantons- und Gemeindesteuern 2019\nTest Muster") == []


def test_steuerrechnung_ohne_jahr_liefert_nichts() -> None:
    assert steuerrechnung("Steuerverwaltung\nRechnungsbetrag CHF 3'210.00") == []


# ------------------------------------------- Mietvertrag, Leistung, Lohnausweis

# Die drei Layouts stellen ihre Zahlen ohne Beschriftung nebeneinander; genau
# darum steht hier je ein Text — die Null-Regel unten prüft ALLE Parser, und
# ohne Text wäre der Parser von ihr ausgenommen.

_MIETVERTRAG = """
Mietvertrag für Wohnräume
Beginn des Mietverhältnisses
1. Juni 2024
6 Mietzins
CHF 1'200.00 CHF 300.00 pauschal CHF 1'500.00
Total Mietzins und Nebenkosten
Der Nettomietzins basiert auf einem Referenzzinssatz von 1.75 % .
"""

_LEISTUNGSABRECHNUNG = """
MUSTERKASSE Leistungsabrechnung
Behandlung
Ihre Jahresfranchise 2021 CHF 2'500.00
Franchise CHF 180.00
Selbstbehalt CHF 20.00
"""

_LOHNAUSWEIS = """
Lohnausweis
E Beginn F Ende der Lohnperiode
01.03.2023 31.08.2023
11 Nettolohn / Salaire net / Salario netto
1'880.00
In die Steuererklärung übertragen
"""


# Spaltentext, nicht Lesereihenfolge — die Form, die dieser eine Parser
# erwartet. Ausführlich geprüft wird er in ``tests/test_anbieter_rechnung.py``; hier steht
# er nur, damit die Runde unten wirklich JEDEN Parser erreicht.



# ----------------------------------------------- Die Null-Regel als ganzer Weg

# Ein Belegtext je Parser. Die Sammlung prüft nicht die einzelnen Werte — das
# tun die Abschnitte oben —, sondern trifft eine Aussage über ALLE Parser
# zugleich: Wer bloss Text liest, darf nie behaupten, die Null gesehen zu haben.
#
# Ohne diese Runde blieb der tragende Teil der Regel ungeprüft. Gemessen: setzt
# man den Standardwert von ``Befund.null_bestaetigt`` auf ``True``, blieben alle
# 661 Tests grün — jeder textgelesene Befund hätte sich dann auf eine gesehene
# Null berufen, und genau der Fehlgriff, gegen den die Regel gebaut ist (auf
# jeder Veranlagung stehen mehrere Nullen nebeneinander), käme durch.
# Hausrat-/Privathaftpflicht-Police, wie OCR sie liefert: ohne Umlaute. Alle
# Betraege erfunden; die ausfuehrlichen Faelle stehen in test_hausrat_police.py.
_HAUSRAT_POLICE = (
    "Hausrat- und Privathaftpflichtversicherung\n"
    "Vertragsbeginn:16.07.2024\n"
    "Pramienubersicht\n"
    "Hausratversicherung-1234Musterstadt,Musterweg1 CHF 200.00\n"
    "Privathaftpflichtversicherung CHF 100.00\n"
    "Versicherungspramie CHF 300.00\n"
    "Stempelabgabe CHF 15.00\n"
    "Jahrespramie CHF 315.00\n"
)


_BELEG_JE_PARSER: dict[str, str] = {
    "kk_praemie": _ABRECHNUNG_EIN_MONAT,
    "strom": _STROM_QUARTAL,
    "pk": _VORSORGEAUSWEIS,
    "police": _POLICE_NEU,
    "verbilligung": _VERBILLIGUNG,
    "steuern": _STEUER_KANTON,
    "miete": _MIETVERTRAG,
    "leistung": _LEISTUNGSABRECHNUNG,
    "lohnausweis": _LOHNAUSWEIS,
    "hausrat": _HAUSRAT_POLICE,
}


def test_zu_jedem_parser_liegt_ein_belegtext_bereit() -> None:
    """Sonst wächst ein neuer Parser an der Null-Regel vorbei."""
    # Anbieterprofile bringen ihren Parser selbst mit; fuer sie gibt es keinen
    # Belegtext im Bestand, sondern eigene Tests am Beispielprofil.
    from moneten.services.belege_parser import PROFILE
    assert set(_BELEG_JE_PARSER) == set(PARSER) - set(PROFILE)


@pytest.mark.parametrize("schluessel", sorted(_BELEG_JE_PARSER))
def test_kein_textgelesener_befund_beruft_sich_auf_eine_gesehene_null(schluessel: str) -> None:
    """Aus Text ist nicht zu erkennen, zu welcher Reihe eine Null gehört.

    ``null_bestaetigt`` ist keine Eigenschaft des Wertes, sondern seiner
    Herkunft: „Ich habe gesehen, dass der Beleg für DIESE Reihe null ausweist."
    Diesen Satz kann nur sagen, wer das Bild vor sich hatte — die Ergänzungsdatei
    von Hand. Jeder Parser hier liest bloss die Textebene und muss die
    Bestätigung deshalb schuldig bleiben.
    """
    befunde = PARSER[schluessel](_BELEG_JE_PARSER[schluessel])
    assert befunde, f"{schluessel}: der Belegtext liefert nichts — der Test misst dann nichts"
    for b in befunde:
        assert b.null_bestaetigt is False, (
            f"{schluessel} → {b.slug}: aus blossem Text gelesen und trotzdem als "
            f"bestätigte Null gemeldet"
        )
        # Die Folge, um die es geht — so ruft das Extraktionsskript die Prüfung
        # (scripts/verlaeufe_aus_scans.py). Mit dieser Herkunft kommt eine 0.00
        # nicht an der Untergrenze der Reihe vorbei.
        if b.slug in _MINDESTWERT:
            assert plausibel(b.slug, Decimal("0"), null_bestaetigt=b.null_bestaetigt) is False
