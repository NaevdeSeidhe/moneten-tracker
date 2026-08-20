"""Gestapelte Balken für Verlaufsreihen mit Positionen.

Der Rechnungsbetrag allein sagt nicht, warum er steigt: eine ausgelaufene
Promotion sieht darin aus wie ein teureres Abo oder ein gekauftes Gerät. Das
Diagramm beantwortet die Frage nur, wenn drei Dinge stimmen — und alle drei
lassen sich still verlieren, ohne dass irgendwo ein Fehler entsteht:

1. **Die Farbe hängt am Namen.** Springt eine Position von Jahr zu Jahr in eine
   andere Farbe, ist „was kommt dazu, was fällt weg" nicht mehr ablesbar.
2. **Die Skala gilt für oben und unten.** Ein Rabatt, der mit eigener Skala
   gezeichnet wird, sieht aus wie ein Betrag in Rechnungshöhe.
3. **Die Bezahlt-Linie ist der echte Wert** und nicht die Balkensumme. Genau
   diese Zahl ist aus den Balken nicht ablesbar.

ALLE DATEN HIER SIND ERFUNDEN. Die Positionsnamen („Abonnement Basis",
„Kleinposten Alpha") und jeder Betrag sind für den Test ausgedacht.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy import select

from moneten.db.models import (
    Category,
    ManagementType,
    MetricCadence,
    MetricKind,
    MetricPoint,
    MetricSeries,
    MetricUnit,
)
from moneten.db.session import SessionLocal
from moneten.palette import chart_colors
from moneten.services.metrics import Punkt, Verlauf, verlauf
from moneten.services.verlauf_positionen import (
    BEZAHLT,
    MIN_OBEN,
    REST_FARBE,
    REST_NAME,
    RUNDUNG_NAME,
    farbzuordnung,
    positionen,
    positions_bild,
)

# Weit in der Zukunft, damit nichts mit Seed- oder anderen Testdaten kollidiert.
JAHR = 2035

# Mehr Positionsnamen, als die Palette Töne hat — bewusst NICHT aus ``MAX_POSTEN``
# abgeleitet: ein Test, der mit der Konstante rechnet, die er prüfen soll, bleibt
# bei jedem Wert grün. Nachgemessen: mit ``MAX_POSTEN = 3`` blieb die ganze Datei
# grün, obwohl fünf Posten still im Sammelposten verschwanden.
VIELE = len(chart_colors()) + 4

STATIC = Path(__file__).resolve().parents[1] / "src" / "moneten" / "static"
CSS = (STATIC / "css" / "theme.css").read_text(encoding="utf-8")
JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")


def _regel(selektor: str) -> dict[str, str]:
    """Die Deklarationen EINER Regel aus ``theme.css``.

    Ein Mini-Scanner statt einer CSS-Bibliothek: gesucht wird der Selektor als
    ganzer Eintrag seiner Selektorliste, Kommentare fallen vorher weg. Mehrere
    Regeln für denselben Selektor werden von links nach rechts überschrieben —
    genau wie im Browser.
    """
    ohne = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    aus: dict[str, str] = {}
    for sel, block in re.findall(r"([^{}@]+)\{([^{}]*)\}", ohne):
        if selektor not in [s.strip() for s in " ".join(sel.split()).split(",")]:
            continue
        for zeile in block.split(";"):
            name, _, wert = zeile.partition(":")
            if wert:
                aus[name.strip()] = " ".join(wert.split())
    return aus


def _js_funktion(name: str) -> str:
    """Rumpf einer Funktion aus ``app.js`` (alles bis zur schliessenden Klammer)."""
    start = JS.index(f"function {name}(")
    return JS[start:JS.index("\n  }", start)]


def _reihe(takt: MetricCadence = MetricCadence.MONATLICH) -> MetricSeries:
    """Eine Reihe ohne DB — der Dienst rechnet, er fragt nicht ab."""
    return MetricSeries(
        slug="erfunden", name="Erfundene Rechnung", unit=MetricUnit.CHF,
        cadence=takt, kind=MetricKind.AUSGABE,
    )


def _punkt(nr: int, positionen_: dict[str, str], wert: str, **rest) -> Punkt:
    extras = {f"pos:{k}": v for k, v in positionen_.items()}
    extras.update(rest.pop("extras", {}))
    return Punkt(
        id=nr, start=date(JAHR, nr, 1), ende=date(JAHR, nr, 28),
        wert=Decimal(wert), neben=None, extras=extras,
        quelle=rest.pop("quelle", "erfundener-beleg.pdf"), notiz=None, diff_pct=None,
    )


def _bild(punkte: list[Punkt], takt: MetricCadence = MetricCadence.MONATLICH):
    return positions_bild(Verlauf(reihe=_reihe(takt), punkte=punkte))


def _werte(balken) -> dict[str, str]:
    """Zeilen einer Periode als Name → Betrag.

    Nur für Fälle OHNE gleichnamige Zeilen brauchbar — die Zeilen selbst tragen
    einen eigenen Schlüssel, gerade weil zwei von ihnen denselben Namen haben
    können (Position „Rundung" neben der ausgewiesenen Rundungsdifferenz).
    """
    return {z.name: z.betrag for z in balken.zeilen}


# ---------------------------------------------------------------------------
# Wann überhaupt Balken
# ---------------------------------------------------------------------------


def test_reihe_ohne_positionen_bekommt_kein_balkenbild() -> None:
    """Prämie, Lohn und Vorsorge behalten ihre Linie.

    ``None`` ist das Signal ans Template. Fiele es weg, bekäme jede Reihe der
    Seite Balken — mit genau einem Band, das nichts aufschlüsselt.
    """
    punkte = [_punkt(1, {}, "342.60"), _punkt(2, {}, "351.20")]
    assert _bild(punkte) is None


def test_eine_einzige_position_genuegt_fuer_balken() -> None:
    assert _bild([_punkt(1, {"Abonnement Basis": "58.00"}, "58.00")]) is not None


def test_leere_reihe_bekommt_kein_balkenbild() -> None:
    assert _bild([]) is None


# ---------------------------------------------------------------------------
# Farbe hängt am Namen
# ---------------------------------------------------------------------------


def test_dieselbe_position_traegt_ueber_alle_perioden_dieselbe_farbe() -> None:
    """Die Kernaussage des Diagramms hängt daran.

    Im ersten Monat steht „Zusatzoption" an zweiter Stelle, im zweiten an
    erster. Färbte die Position nach ihrem PLATZ im Stapel, wechselte sie
    dadurch die Farbe — und der Verlauf einer Position wäre nicht zu verfolgen.
    """
    bild = _bild([
        _punkt(1, {"Abonnement Basis": "58.00", "Zusatzoption": "14.50"}, "72.50"),
        _punkt(2, {"Zusatzoption": "16.00"}, "16.00"),
    ])
    farben = [
        {s.name: s.farbe for s in b.oben} for b in bild.balken
    ]
    assert farben[0]["Zusatzoption"] == farben[1]["Zusatzoption"]


def test_die_stapelreihenfolge_ist_ueber_alle_perioden_dieselbe() -> None:
    """Ein Band, das von Monat zu Monat die Höhe im Stapel wechselt, ist mit
    dem Auge nicht zu verfolgen."""
    bild = _bild([
        _punkt(1, {"Beta": "10.00", "Alpha": "20.00"}, "30.00"),
        _punkt(2, {"Alpha": "20.00", "Beta": "12.00"}, "32.00"),
    ])
    assert [s.name for s in bild.balken[0].oben] == [s.name for s in bild.balken[1].oben]


def test_die_farbe_folgt_der_alphabetischen_reihenfolge() -> None:
    """Nachvollziehbar statt gewürfelt: die Legende steht in Farbreihenfolge.

    Über einen Hash wäre die Zuordnung nicht erklärbar — und eine neu
    dazukommende Position würfelte die Farben der bestehenden durcheinander.
    """
    gewicht = {"Zulu": Decimal("5"), "Alpha": Decimal("9"), "Mike": Decimal("7")}
    assert farbzuordnung(gewicht) == dict(
        zip(["Alpha", "Mike", "Zulu"], chart_colors()[:3], strict=True)
    )


def test_die_groessten_posten_bekommen_die_eigenen_farben() -> None:
    """Mehr Namen als Palettentöne: die kleinen fallen in den Sammelposten.

    Nach Gewicht ausgewählt und nicht alphabetisch abgeschnitten — sonst
    verlöre ausgerechnet ein Gerätekauf am Ende des Alphabets seine Farbe und
    verschwände im Sammelposten, obwohl er den Balken in die Höhe treibt.

    Gezählt wird gegen die PALETTE und nicht gegen ``MAX_POSTEN``: so viele
    Namen bekommen eine eigene Farbe, wie es Töne gibt. Mit der Konstante
    gerechnet, bliebe der Test bei jedem ihrer Werte grün.
    """
    pos = {f"Position {i:02d}": "1.00" for i in range(VIELE)}
    pos["Position 99"] = "500.00"  # alphabetisch zuletzt, aber der grösste
    bild = _bild([_punkt(1, pos, str(sum(Decimal(w) for w in pos.values())))])

    namen = [p.name for p in bild.posten]
    assert "Position 99" in namen
    assert REST_NAME in namen
    bunt = [p for p in bild.posten if p.farbe in chart_colors()]
    assert len(bunt) == len(chart_colors())
    assert len(namen) == len(chart_colors()) + 1


def test_der_sammelposten_bleibt_unbunt() -> None:
    """Er ist kein Posten, sondern ein Rest — und verbraucht darum keine Farbe,
    die einem echten Posten zusteht."""
    pos = {f"Position {i:02d}": "1.00" for i in range(VIELE)}
    bild = _bild([_punkt(1, pos, str(sum(Decimal(w) for w in pos.values())))])
    rest = next(p for p in bild.posten if p.name == REST_NAME)
    assert rest.farbe == REST_FARBE
    assert rest.farbe not in chart_colors()


def test_kleine_posten_behalten_im_aufklapper_ihren_namen() -> None:
    """Im Balken zusammengefasst, in der Zeile einzeln — sonst wäre der
    Sammelposten eine Sackgasse."""
    pos = {f"Position {i:02d}": "1.00" for i in range(VIELE)}
    bild = _bild([_punkt(1, pos, str(sum(Decimal(w) for w in pos.values())))])
    assert set(pos) <= set(_werte(bild.balken[0]))
    assert {p.name for p in bild.zeilen_vorlage} >= set(pos)


# ---------------------------------------------------------------------------
# Oben und unten
# ---------------------------------------------------------------------------


def test_rabatte_stehen_unter_der_nulllinie() -> None:
    bild = _bild([_punkt(1, {"Abo": "60.00", "Aktionsrabatt": "-9.00"}, "51.00")])
    b = bild.balken[0]
    assert [s.name for s in b.oben] == ["Abo"]
    assert [s.name for s in b.unten] == ["Aktionsrabatt"]
    assert b.unten[0].betrag == Decimal("9.00")  # Fläche, kein Vorzeichen
    assert b.unten[0].rabatt


def test_ein_posten_der_nur_rabatt_ist_wird_in_der_legende_markiert() -> None:
    """Sein Muster gehört schraffiert — er ist über der Nulllinie nie zu sehen."""
    bild = _bild([_punkt(1, {"Abo": "60.00", "Aktionsrabatt": "-9.00"}, "51.00")])
    nach_name = {p.name: p for p in bild.posten}
    assert nach_name["Aktionsrabatt"].nur_rabatt
    assert not nach_name["Abo"].nur_rabatt


def test_oben_und_unten_teilen_sich_eine_skala() -> None:
    """Sonst sähe ein Rabatt von 12 aus wie eine Rechnung von 120.

    Gerechnet wird die Probe umgekehrt zur Darstellung: aus den Zeilenanteilen
    und den Prozenthöhen muss wieder derselbe Betrag je Pixel herauskommen.
    """
    bild = _bild([
        _punkt(1, {"Abo": "100.00", "Rabatt": "-40.00"}, "60.00"),
        _punkt(2, {"Abo": "100.00", "Rabatt": "-20.00"}, "80.00"),
    ])
    oben, unten = bild.anteil_oben / 100, (100 - bild.anteil_oben) / 100
    # „Pixel" je Franken in beiden Zeilen, für eine gedachte Gesamthöhe 1.
    je_franken_oben = bild.balken[0].h_oben / 100 * oben / float(bild.balken[0].brutto)
    je_franken_unten = bild.balken[0].h_unten / 100 * unten / float(bild.balken[0].rabatt)
    assert je_franken_oben == pytest.approx(je_franken_unten, rel=1e-3)


def test_die_rabattzeile_behaelt_ihre_skala_auch_mit_luft() -> None:
    """``MIN_UNTEN`` gibt der Zeile Platz für ihre Beschriftung — nicht mehr.

    Würde die Zeile stattdessen auf den grössten Rabatt normiert, wäre die
    gemeinsame Skala genau dort aufgegeben, wo sie am meisten zählt: im Monat
    mit dem Ausreisser.
    """
    bild = _bild([
        _punkt(1, {"Abo": "80.00", "Rabatt": "-12.00"}, "68.00"),
        _punkt(2, {"Abo": "80.00", "Geraetekauf": "649.00"}, "729.00"),
    ])
    oben, unten = bild.anteil_oben / 100, (100 - bild.anteil_oben) / 100
    # Feste Schranken statt der importierten Konstante: mit ihr gerechnet bliebe
    # der Test bei jedem Wert grün — nachgemessen auch bei 0.45, wo die Rabatt-
    # zeile fast die halbe Fläche nähme. Unten: die Zeile ist 148 px hoch (Handy)
    # und muss ihre Beschriftung tragen. Oben: der Rabatt ist hier ein Sechzigstel
    # des grössten Balkens, die Zeile darf nicht aussehen wie die Hauptsache.
    assert 0.10 <= unten <= 0.20, f"Rabattzeile bei {unten:.3f} der Fläche"
    je_franken_oben = bild.balken[1].h_oben / 100 * oben / float(bild.balken[1].brutto)
    je_franken_unten = bild.balken[0].h_unten / 100 * unten / float(bild.balken[0].rabatt)
    assert je_franken_oben == pytest.approx(je_franken_unten, rel=1e-3)


def test_die_rabattmarke_sitzt_auf_der_tiefe_des_groessten_rabatts() -> None:
    """Am Zeilenrand benennte sie einen Wert, den es nicht gibt."""
    ohne_luft = _bild([_punkt(1, {"Abo": "60.00", "Rabatt": "-20.00"}, "40.00")])
    mit_luft = _bild([
        _punkt(1, {"Abo": "80.00", "Rabatt": "-2.00"}, "78.00"),
        _punkt(2, {"Abo": "80.00", "Geraetekauf": "649.00"}, "729.00"),
    ])
    assert ohne_luft.marke_unten == pytest.approx(100.0)
    assert mit_luft.marke_unten < 100.0


def test_ohne_rabatt_gibt_es_keine_untere_zeile() -> None:
    """Ein leerer Rahmen unter der Nulllinie liest sich als Fehler."""
    bild = _bild([_punkt(1, {"Abo": "60.00"}, "60.00")])
    assert not bild.zeigt_rabatt
    assert bild.anteil_oben == 100.0


# ---------------------------------------------------------------------------
# Bezahlt-Linie
# ---------------------------------------------------------------------------


def test_die_linie_zeigt_den_bezahlten_betrag_und_nicht_die_balkensumme() -> None:
    """Die eine Zahl, die aus den Balken nicht ablesbar ist.

    Wäre sie die Brutto-Summe, liefe sie exakt auf den Balkenkanten und trüge
    keine eigene Information.
    """
    bild = _bild([_punkt(1, {"Abo": "100.00", "Rabatt": "-40.00"}, "60.00")])
    b = bild.balken[0]
    assert b.bezahlt == Decimal("60.00")
    # 60 von 100 → 40 % unter dem oberen Rand.
    assert b.y == pytest.approx(40.0, abs=0.01)
    assert b.h_oben == pytest.approx(100.0)


def test_die_linie_hat_gerade_segmente() -> None:
    """Zwischen zwei Rechnungen gibt es keine Zwischenwerte; eine geglättete
    Kurve behauptete welche — und liefe durch Beträge, die nie berechnet wurden."""
    bild = _bild([
        _punkt(1, {"Abo": "60.00"}, "60.00"),
        _punkt(2, {"Abo": "90.00"}, "90.00"),
        _punkt(3, {"Abo": "70.00"}, "70.00"),
    ])
    assert re.fullmatch(r"M [\d.]+ [\d.]+ L [\d.]+ [\d.]+ L [\d.]+ [\d.]+", bild.pfad), bild.pfad
    assert "C" not in bild.pfad and "Q" not in bild.pfad


def test_die_spaltenmitten_liegen_bei_der_haelfte_ihrer_spalte() -> None:
    """Das Balkenraster läuft ohne Spalt — nur dann steht der Linienpunkt
    wirklich über seinem Balken."""
    bild = _bild([_punkt(i, {"Abo": "60.00"}, "60.00") for i in (1, 2, 3, 4)])
    assert [b.x for b in bild.balken] == [12.5, 37.5, 62.5, 87.5]


# ---------------------------------------------------------------------------
# Rundung, unaufgeschlüsselte und unbestätigte Perioden
# ---------------------------------------------------------------------------


def test_die_rundung_steht_im_aufklapper_und_nicht_im_balken() -> None:
    """Sie ist keine Leistung — als Band stünde dort ein Posten, den es nicht
    gibt. Im Aufklapper muss sie stehen, sonst ergeben die Zeilen nicht den
    Betrag, der darunter als bezahlt ausgewiesen ist."""
    bild = _bild([_punkt(1, {"Abo": "60.02"}, "60.00", extras={"rundung": "-0.02"})])
    b = bild.balken[0]
    assert _werte(b)[RUNDUNG_NAME] == "−0.02"
    assert RUNDUNG_NAME not in [s.name for s in b.oben + b.unten]
    assert RUNDUNG_NAME not in [p.name for p in bild.posten]
    assert RUNDUNG_NAME in [p.name for p in bild.zeilen_vorlage]


def test_die_zeilen_einer_periode_ergeben_den_bezahlten_betrag() -> None:
    """Die Probe, die der Nutzer im Werte-Kasten selbst nachrechnen kann."""
    bild = _bild([
        _punkt(1, {"Abo": "58.00", "Option": "14.50", "Rabatt": "-4.50"}, "68.00"),
    ])
    zeilen = bild.balken[0].zeilen
    summe = sum(
        Decimal(z.betrag.replace("−", "-").replace("'", ""))
        for z in zeilen if z.schluessel != BEZAHLT
    )
    bezahlt = next(z for z in zeilen if z.schluessel == BEZAHLT)
    assert summe == Decimal(bezahlt.betrag.replace("'", ""))


def test_eine_position_mit_null_steht_in_der_zeile_aber_nicht_im_balken() -> None:
    """„Steht mit 0.00 auf der Rechnung" ist etwas anderes als „kommt nicht vor".

    Nur die Zeile kann das sagen; ein Band der Höhe null lässt sich nicht
    zeichnen.
    """
    bild = _bild([
        _punkt(1, {"Abo": "60.00", "Auslandsgespraeche": "12.00"}, "72.00"),
        _punkt(2, {"Abo": "60.00", "Auslandsgespraeche": "0.00"}, "60.00"),
    ])
    b = bild.balken[1]
    assert _werte(b)["Auslandsgespraeche"] == "0.00"
    assert "Auslandsgespraeche" not in [s.name for s in b.oben]


def test_periode_ohne_positionen_wird_offen_gezeichnet() -> None:
    """Von Hand nachgetragen: die Höhe ist bekannt, die Zusammensetzung nicht.

    Ein gefülltes Band behauptete eine Position, die niemand kennt — und ein
    leerer Balken behauptete eine Null.
    """
    bild = _bild([
        _punkt(1, {"Abo": "60.00"}, "60.00"),
        _punkt(2, {}, "71.40", quelle=None),
    ])
    offen = bild.balken[1]
    assert offen.offen
    assert offen.oben == []
    assert offen.h_oben > 0
    assert bild.hat_offene


def test_unbestaetigte_periode_bleibt_markiert() -> None:
    """Dieselbe Zusage wie beim gestrichelten Messpunkt der Linien-Diagramme."""
    bild = _bild([_punkt(1, {"Abo": "60.00"}, "60.00", extras={"unsicher": "1"})])
    assert bild.balken[0].unsicher
    assert bild.hat_unsichere


def test_ein_unlesbarer_positionsbetrag_reisst_das_zeichnen_nicht_ab() -> None:
    """Der Import lässt so etwas nicht durch; von Hand in der DB geändert wäre
    eine Fehlerseite beim Zeichnen die schlechtere Antwort."""
    punkt = _punkt(1, {"Abo": "60.00", "Kaputt": "ca. 20"}, "60.00")
    assert positionen(punkt) == {"Abo": Decimal("60.00")}
    assert _bild([punkt]) is not None


def test_gleichnamige_positionen_werden_addiert() -> None:
    """Zwei Zeilen desselben Namens sind ein Posten, nicht zwei Bänder."""
    punkt = Punkt(
        id=1, start=date(JAHR, 1, 1), ende=date(JAHR, 1, 31), wert=Decimal("30.00"),
        neben=None, extras={"pos:Abo": "10.00", "pos:Abo:Zusatz": "20.00"},
        quelle=None, notiz=None, diff_pct=None,
    )
    # Getrennt wird am ERSTEN Doppelpunkt — „Abo:Zusatz" ist ein eigener Name.
    assert positionen(punkt) == {"Abo": Decimal("10.00"), "Abo:Zusatz": Decimal("20.00")}


# ---------------------------------------------------------------------------
# Steht es auch auf der Seite?
# ---------------------------------------------------------------------------
#
# Der Grund für diesen Abschnitt: der Soll/Ist-Abgleich existierte in diesem
# Projekt eine Zeit lang als Dienst samt grüner Tests, war aber an kein Template
# angebunden. Alles war grün, und auf der Seite stand nichts.


@pytest.fixture
def gebaute_reihen():
    """Zwei Reihen: eine mit Positionen, eine ohne — und räumt beide weg.

    Die zweite ist die Gegenprobe „nichts Bestehendes kaputtgemacht". Sie muss
    hier entstehen und darf nicht eine geseedete sein: die haben in der
    Test-Datenbank keine Punkte und zeichnen darum gar kein Diagramm.
    """
    marke = uuid.uuid4().hex[:8]
    gebaut: list[tuple[int, int | None]] = []
    with SessionLocal() as db:
        kat = Category(name=f"Erfundene Kategorie {marke}",
                       management_type=ManagementType.DAUERAUFTRAG)
        db.add(kat)
        db.flush()
        mit = MetricSeries(
            slug=f"balken_{marke}", name=f"Erfundene Rechnung {marke}",
            unit=MetricUnit.CHF, cadence=MetricCadence.MONATLICH,
            kind=MetricKind.AUSGABE, category_id=kat.id,
        )
        ohne = MetricSeries(
            slug=f"linie_{marke}", name=f"Erfundene Prämie {marke}",
            unit=MetricUnit.CHF, cadence=MetricCadence.MONATLICH,
            kind=MetricKind.AUSGABE,
        )
        db.add_all([mit, ohne])
        db.flush()
        db.add(MetricPoint(
            series_id=mit.id, period_start=date(JAHR, 3, 1), period_end=date(JAHR, 3, 31),
            value=Decimal("68.00"), source="erfundener-beleg.pdf",
            extras={"pos:Abonnement Basis": "58.00", "pos:Zusatzoption": "14.50",
                    "pos:Aktionsrabatt": "-4.50"},
        ))
        for monat in (3, 4):
            db.add(MetricPoint(
                series_id=ohne.id, period_start=date(JAHR, monat, 1),
                period_end=date(JAHR, monat, 28), value=Decimal("342.60"),
            ))
        db.commit()
        gebaut = [(mit.id, kat.id), (ohne.id, None)]
        daten = (mit.slug, mit.id, ohne.slug)

    yield daten

    with SessionLocal() as db:
        for reihe_id, kat_id in gebaut:
            if (obj := db.get(MetricSeries, reihe_id)) is not None:
                db.delete(obj)  # nimmt die Punkte per cascade mit
            if kat_id is not None and (k := db.get(Category, kat_id)) is not None:
                db.delete(k)
        db.commit()


def _karte(html: str, slug: str) -> str:
    """Der Kartenausschnitt EINER Reihe — sonst prüft man die Nachbarkarte mit.

    Aufgeteilt an den Karten und dann die gesuchte herausgegriffen: die
    Vorlage schreibt den Slug erst weit UNTEN in die Formular-Ziele, ein
    Ausschnitt „ab dem ersten Vorkommen" liesse das Diagramm draussen.
    """
    karten = html.split('<article class="card vl-karte">')
    treffer = [k for k in karten if f"/verlaeufe/{slug}/punkt" in k]
    assert len(treffer) == 1, f"{len(treffer)} Karten für {slug}"
    return treffer[0]


def _block(html: str, klasse: str, tag: str = "div") -> str:
    """Genau EIN Element mit dieser Klasse samt Inhalt, an den Klammern gezählt.

    Ein `.*?</div>` reichte nicht: der Block enthält selbst ``<div>``, und das
    erste Kind-Ende beendete den Ausschnitt. Genau daran verlor der Test zur
    Kappungszeile seine Aussage — der alte Ausdruck suchte bis zum nächsten
    ``</p>``, und als daraus ein ``<div>`` wurde, umfasste der Treffer stumm die
    halbe Karte und blieb grün.

    Die Klasse wird als EINZELNE Klasse im Attribut gesucht, nicht als ganzes
    Attribut: ``class="disclosure vb-kappung"`` ist derselbe Block wie
    ``class="vb-kappung"``. Vorher hing der Helfer am exakten Attributtext und
    fand den Block nicht mehr, sobald eine zweite Klasse dazukam — ein Test, der
    an der Reihenfolge von Klassennamen zerbricht, prüft die falsche Sache.
    """
    treffer = re.search(rf'class="[^"]*\b{re.escape(klasse)}\b[^"]*"', html)
    assert treffer, f"Kein Element mit der Klasse {klasse}"
    start = html.rindex(f"<{tag}", 0, treffer.start())
    tiefe = 0
    for m in re.finditer(rf"<(/?){tag}\b", html[start:]):
        tiefe += -1 if m.group(1) else 1
        if tiefe == 0:
            return html[start:start + m.start()]
    raise AssertionError(f"{klasse}: kein schliessendes </{tag}>")


def test_die_seite_zeichnet_balken_statt_linie(
    logged_in_client: TestClient, gebaute_reihen
) -> None:
    html = logged_in_client.get("/verlaeufe").text
    karte = _karte(html, gebaute_reihen[0])
    assert 'class="vb-canvas"' in karte, "Kein Balkendiagramm im Markup"
    assert 'class="vl-linien"' not in karte, "Die Linie steht noch daneben"
    assert "Abonnement Basis" in karte
    assert "Aktionsrabatt" in karte


def test_die_werteliste_zeigt_die_positionen(
    logged_in_client: TestClient, gebaute_reihen
) -> None:
    """Das Diagramm sagt im aria-label zu, alle Zahlen stünden in der Liste
    darunter. Ohne die Positionen dort wäre die Zusage falsch — und ohne
    Zeigegerät gäbe es die Aufschlüsselung nirgends zu lesen."""
    html = logged_in_client.get("/verlaeufe").text
    karte = _karte(html, gebaute_reihen[0])
    zeile = re.search(r'vb-zeile-pos">(.*?)</div>', karte, re.S)
    assert zeile, "Keine Positionszeile in der Werteliste"
    assert "Abonnement Basis" in zeile.group(1)
    assert "58.00" in zeile.group(1)
    assert "−4.50" in zeile.group(1)


def test_reihen_ohne_positionen_behalten_die_linie(
    logged_in_client: TestClient, gebaute_reihen
) -> None:
    """Nichts Bestehendes kaputtmachen — die Linie muss bleiben, wo sie war."""
    html = logged_in_client.get("/verlaeufe").text
    karte = _karte(html, gebaute_reihen[2])
    assert 'class="vl-linien"' in karte, "Das Linien-Diagramm ist verschwunden"
    assert "vb-canvas" not in karte, "Eine Reihe ohne Positionen bekam Balken"


def test_das_diagramm_traegt_die_beschreibung_fuer_screenreader(
    logged_in_client: TestClient, gebaute_reihen
) -> None:
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebaute_reihen[0])
    assert "nach Positionen:" in karte
    assert "Alle Zahlen stehen in der Werteliste darunter." in karte


def test_der_dienst_liest_die_reihe_aus_der_datenbank(gebaute_reihen) -> None:
    """Gegenprobe zum Rest der Datei: dort werden ``Punkt``-Objekte gebaut,
    hier kommt der Verlauf aus der DB — samt der JSON-Spalte ``extras``."""
    with SessionLocal() as db:
        r = db.scalar(select(MetricSeries).where(MetricSeries.slug == gebaute_reihen[0]))
        bild = positions_bild(verlauf(db, r))
    assert bild is not None
    # Legende in Stapelreihenfolge, unten der kleinste Einzelbetrag: 4.50
    # Aktionsrabatt, 14.50 Zusatzoption, 58.00 Abonnement Basis.
    assert [p.name for p in bild.posten] == [
        "Aktionsrabatt", "Zusatzoption", "Abonnement Basis",
    ]


# ---------------------------------------------------------------------------
# Die Höhe der Bänder
# ---------------------------------------------------------------------------
#
# Aus dieser Zahl entsteht die Aussage eines gestapelten Balkens. Sie kam in
# keinem Test vor: nachgemessen blieb die ganze Suite grün, als jedes Band die
# gleiche Höhe bekam (``anteil = 100 / len(posten)``) — der Balken zeigte dann
# eine Zusammensetzung, die es nicht gibt.


def test_die_bandhoehen_folgen_den_betraegen() -> None:
    """Dreimal so teuer heisst dreimal so hoch."""
    bild = _bild([_punkt(1, {"Abo": "75.00", "Option": "25.00"}, "100.00")])
    anteile = {s.name: s.anteil for s in bild.balken[0].oben}
    assert anteile == {"Abo": 75.0, "Option": 25.0}


def test_die_bandhoehen_ergeben_zusammen_den_ganzen_stapel() -> None:
    """Sonst bliebe im Balken ein Rest übrig, der keiner Position gehört."""
    bild = _bild([_punkt(1, {"A": "13.00", "B": "27.00", "C": "60.00"}, "100.00")])
    assert sum(s.anteil for s in bild.balken[0].oben) == pytest.approx(100.0, abs=0.05)


def test_der_groesste_einzelbetrag_liegt_oben_im_stapel() -> None:
    """Abgeschnitten wird an der Decke, also OBEN.

    Läge dort ein laufender Posten, verschwände er im Ausreissermonat aus dem
    Bild, während der einmalige Grossposten als einziges Band stehen bliebe —
    gemessen bestand der gekappte Balken zu 100 % aus dem Gerät. Unten die
    kleinen: so bleibt der Monat mit seinen Nachbarn vergleichbar.
    """
    bild = _bild([
        _punkt(1, {"Grundgebuehr": "80.00", "Kleinposten": "5.00"}, "85.00"),
        _punkt(2, {"Grundgebuehr": "80.00", "Kleinposten": "5.00",
                   "Erfundenes Faltgeraet": "1200.00"}, "1285.00"),
    ])
    assert [s.name for s in bild.balken[0].oben] == ["Kleinposten", "Grundgebuehr"]
    assert [s.name for s in bild.balken[1].oben][-1] == "Erfundenes Faltgeraet"


# ---------------------------------------------------------------------------
# Achsendeckel und Schnittmarke
# ---------------------------------------------------------------------------


def _laufend(anzahl: int = 8) -> list[Punkt]:
    """Perioden ohne Ausreisser — erfundene, gleichbleibende Beträge."""
    return [
        _punkt(i, {"Grundgebuehr": "80.00", "Zusatzoption": "15.00"}, "95.00")
        for i in range(1, anzahl + 1)
    ]


def test_ohne_ausreisser_bleibt_die_achse_beim_hoechstwert() -> None:
    """Eine Schnittmarke ist ein starkes Zeichen; ohne Not gesetzt, ist sie Lärm."""
    bild = _bild(_laufend())
    assert bild.deckel == Decimal("95.00")
    assert not bild.kappungen
    assert not any(b.gekappt for b in bild.balken)


def test_wenige_perioden_werden_nicht_gedeckelt() -> None:
    """Unter fünf Perioden gibt es keinen laufenden Fall, gegen den ein
    Ausreisser einer wäre."""
    punkte = _laufend(3)
    punkte.append(_punkt(4, {"Grundgebuehr": "80.00",
                             "Erfundenes Faltgeraet": "1200.00"}, "1280.00"))
    bild = _bild(punkte)
    assert bild.deckel == Decimal("1280.00")
    assert not bild.kappungen


def test_ein_einmaliger_grossposten_deckelt_die_achse() -> None:
    """Der Kern des Wunsches: die laufenden Monate müssen unterscheidbar bleiben.

    Ohne Deckel bekäme ein 95er-Monat neben einem 1280er-Monat 7 % der Fläche —
    alle Monate gleich flach, der Vergleich zwischen ihnen unmöglich. Mit Deckel
    füllt er über die Hälfte.
    """
    punkte = _laufend()
    punkte.append(_punkt(9, {"Grundgebuehr": "80.00",
                             "Erfundenes Faltgeraet": "1200.00"}, "1280.00"))
    bild = _bild(punkte)
    assert bild.deckel < Decimal("1280.00")
    assert bild.balken[0].h_oben > 50.0
    # Und die Skala bleibt eine: der Deckel IST die volle Zeilenhöhe.
    assert bild.balken[-1].h_oben == pytest.approx(100.0)


def test_der_monat_ueber_der_decke_wird_bis_zur_decke_gezeichnet_und_gekappt() -> None:
    """Er endet nicht einfach an der Kante — dann behauptete er diese Höhe."""
    punkte = _laufend()
    punkte.append(_punkt(9, {"Grundgebuehr": "80.00",
                             "Erfundenes Faltgeraet": "1200.00"}, "1280.00"))
    bild = _bild(punkte)
    hoch = bild.balken[-1]
    assert hoch.h_oben == pytest.approx(100.0)
    assert hoch.gekappt
    assert not bild.balken[0].gekappt


def test_was_ueber_der_achse_liegt_wird_mit_namen_und_betrag_benannt() -> None:
    """„Benannt statt gezeichnet": der Positionsname kommt wörtlich von der
    Rechnung, dort steht also das Gerät."""
    punkte = _laufend()
    punkte.append(_punkt(9, {"Grundgebuehr": "80.00",
                             "Erfundenes Faltgeraet": "1200.00"}, "1280.00"))
    bild = _bild(punkte)
    assert len(bild.kappungen) == 1
    k = bild.kappungen[0]
    assert k.label == bild.balken[-1].label
    assert ("Erfundenes Faltgeraet", "1'200.00") in k.posten
    assert not k.posten_unten


def test_die_baender_unter_der_schnittkante_behalten_die_skala_der_uebrigen() -> None:
    """Der Grund gegen eine gestauchte Achse: gestaucht addierten sich die
    Bänder nicht mehr zur Balkenhöhe, und die Zusammensetzung wäre falsch
    dargestellt. Gekappt behält jedes gezeichnete Band seine Pixel je Franken.
    """
    punkte = _laufend()
    punkte.append(_punkt(9, {"Grundgebuehr": "80.00", "Zusatzoption": "15.00",
                             "Erfundenes Faltgeraet": "1200.00"}, "1295.00"))
    bild = _bild(punkte)

    def je_prozent(balken, name: str, betrag: float) -> float:
        """Franken je Prozentpunkt der ZEILENhöhe, gemessen an einem Band."""
        anteil = next(s.anteil for s in balken.oben if s.name == name)
        return betrag / (anteil * balken.h_oben / 100)

    # Dieselbe Position, einmal im laufenden und einmal im gekappten Monat.
    assert je_prozent(bild.balken[0], "Grundgebuehr", 80.0) == pytest.approx(
        je_prozent(bild.balken[-1], "Grundgebuehr", 80.0), rel=1e-2)


def test_ein_einmaliger_grossrabatt_wird_unten_gekappt_und_benannt() -> None:
    """Dieselbe Regel unter der Nulllinie: eine Altgerät-Rückgabe presste die
    laufenden Rabatte sonst auf null."""
    punkte = [
        _punkt(i, {"Grundgebuehr": "80.00", "Aktionsrabatt": "-5.00"}, "75.00")
        for i in range(1, 9)
    ]
    punkte.append(_punkt(9, {"Grundgebuehr": "80.00",
                             "Rabatt Altgeraet": "-200.00"}, "-120.00"))
    bild = _bild(punkte)
    assert bild.deckel_unten < Decimal("200.00")
    assert bild.balken[-1].gekappt_unten
    unten = [k for k in bild.kappungen if k.posten_unten]
    assert unten and ("Rabatt Altgeraet", "−200.00") in unten[0].posten_unten


def test_ein_rabatt_reicht_nie_tiefer_als_die_achse_hoch_ist() -> None:
    """Trägt nur EINE Periode einen Rabatt, gibt es keinen laufenden Wert, gegen
    den er ein Ausreisser wäre — der Deckel bleibt dann bei seiner Tiefe. Eine
    Rückgabe-Gutschrift nahm damit gemessen 57 % der Zeichenfläche für eine
    einzige Periode. Sie wird stattdessen gekappt und benannt wie ein Balken
    über der Decke.
    """
    punkte = [_punkt(i, {"Grundgebuehr": "80.00", "Zusatzoption": "15.00"}, "95.00")
              for i in range(1, 7)]
    punkte.append(_punkt(7, {"Grundgebuehr": "80.00", "Zusatzoption": "15.00",
                             "Rabatt Altgeraet": "-200.00"}, "-105.00"))
    bild = _bild(punkte)
    assert bild.deckel_unten <= bild.deckel
    assert bild.anteil_oben >= 50.0, "Die Rabattzeile nimmt mehr als die Hälfte"
    assert bild.balken[-1].gekappt_unten
    assert bild.kappungen[0].posten_unten == [("Rabatt Altgeraet", "−200.00")]


# ---------------------------------------------------------------------------
# Wenn nichts über der Nulllinie steht
# ---------------------------------------------------------------------------


def test_eine_reihe_nur_aus_rabatten_behaelt_eine_obere_zeile() -> None:
    """Sonst ist die obere Zeile 0 px hoch.

    Gemessen: die Nulllinie lag am oberen Rand des Zeichenfelds, ihre „0" auf
    derselben Grundlinie wie die Zahl der Rabattzeile — drei Achsenzahlen
    ineinander über einem leeren Rechteck.
    """
    bild = _bild([_punkt(1, {"Aktionsrabatt": "-12.00"}, "0.00")])
    assert not bild.zeigt_oben, "Eine Achsenzahl 0.00 über der Nulllinie"
    assert bild.anteil_oben >= MIN_OBEN * 100
    assert bild.balken[0].h_unten == pytest.approx(100.0)
    assert bild.marke_unten == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Die Bezahlt-Marke
# ---------------------------------------------------------------------------


def test_eine_rundung_nach_oben_hebt_die_linie_nicht_aus_dem_bild() -> None:
    """Die Bezahlt-Linie geht in die obere Achse ein.

    Bei einer ausgewiesenen Rundung nach oben liegt sie über dem Brutto-Balken;
    eine Achse, die nur die Balken kennt, schnitte sie am oberen Rand ab.
    """
    bild = _bild([_punkt(1, {"Abo": "60.00"}, "60.05", extras={"rundung": "0.05"})])
    b = bild.balken[0]
    assert b.y >= 0.0
    assert not b.y_aus, "Die Marke wurde am oberen Rand geklemmt"


def test_ein_negativ_bezahlter_monat_bleibt_im_bild_und_wird_benannt() -> None:
    """Eine Gutschrift schob die Marke aus dem Zeichenfeld (gemessen: 141.67 %
    Höhe, 4 px unter der Unterkante), und die Linie brach dort ab — ohne dass
    irgendetwas den Grund nannte."""
    bild = _bild([
        _punkt(1, {"Abo": "60.00"}, "60.00"),
        _punkt(2, {"Abo": "60.00", "Gutschrift": "-85.00"}, "-25.00"),
    ])
    b = bild.balken[1]
    assert 0.0 <= b.y <= 100.0
    assert b.y_aus
    assert "L 75.0 100.0" in bild.pfad, bild.pfad
    assert [k.bezahlt for k in bild.kappungen] == ["−25.00"]


def test_auch_der_bezahlte_betrag_traegt_das_echte_minus() -> None:
    """Im selben Kasten standen zwei verschiedene Minuszeichen: die Rabattzeilen
    mit U+2212, die Summe darunter mit dem Bindestrich der Tastatur."""
    bild = _bild([_punkt(1, {"Gutschrift": "-25.00"}, "-25.00")])
    zeilen = bild.balken[0].zeilen
    bezahlt = next(z for z in zeilen if z.schluessel == BEZAHLT)
    assert bezahlt.betrag == "−25.00"
    assert "-" not in bezahlt.betrag


# ---------------------------------------------------------------------------
# Zeilen des Werte-Kastens
# ---------------------------------------------------------------------------


def test_eine_position_die_immer_null_ist_wird_nicht_als_rabatt_markiert() -> None:
    """Sie kommt nie positiv vor — das allein machte sie zum Rabatt.

    Sie bekam damit in Legende und Kasten das Schraffurmuster mit der Bedeutung
    „wird abgezogen", obwohl sie in keiner Periode etwas abzieht.
    """
    bild = _bild([
        _punkt(1, {"Abo": "60.00", "Nulloption": "0.00"}, "60.00"),
        _punkt(2, {"Abo": "60.00", "Nulloption": "0.00"}, "60.00"),
    ])
    nach_name = {p.name: p for p in bild.posten}
    assert not nach_name["Nulloption"].nur_rabatt
    assert not {p.name: p for p in bild.zeilen_vorlage}["Nulloption"].nur_rabatt


def test_eine_position_namens_rundung_verdraengt_die_rundungsdifferenz_nicht() -> None:
    """Namenskollision: gemessen fiel dabei ein Positionsbetrag aus dem Kasten.

    Die Zeilen ergaben danach nicht mehr den bezahlten Betrag — genau die Probe,
    die der Nutzer im Kasten selbst nachrechnen kann.
    """
    bild = _bild([_punkt(1, {"Abo": "60.00", RUNDUNG_NAME: "5.00"}, "64.98",
                         extras={"rundung": "-0.02"})])
    zeilen = bild.balken[0].zeilen
    betraege = sorted(z.betrag for z in zeilen if z.name == RUNDUNG_NAME)
    assert betraege == ["5.00", "−0.02"]
    # Jede Zeile hat ihren eigenen Schlüssel, sonst zeigten beide denselben Wert.
    assert len({z.schluessel for z in zeilen}) == len(zeilen)
    summe = sum(Decimal(z.betrag.replace("−", "-").replace("'", ""))
                for z in zeilen if z.schluessel != BEZAHLT)
    assert summe == Decimal("64.98")


def test_der_kasten_stellt_erst_die_kosten_und_dann_die_abzuege() -> None:
    """Buchhalterisch untereinander: so lässt sich der bezahlte Betrag von oben
    nach unten nachrechnen.

    Der Rabatt ist hier grösser als jede einzelne Kostenposition — nach Betrag
    sortiert stünde er zuoberst, und der Kasten begänne mit einem Abzug von
    etwas, das noch gar nicht dasteht.
    """
    bild = _bild([
        _punkt(1, {"Grundgebuehr": "80.00", "Rueckgabe Altgeraet": "-200.00",
                   "Zusatzoption": "15.00"}, "-105.00"),
    ])
    namen = [p.name for p in bild.zeilen_vorlage]
    assert namen == ["Grundgebuehr", "Zusatzoption", "Rueckgabe Altgeraet"]


def test_das_markup_bekommt_die_zeilen_je_periode() -> None:
    """``als_json`` ist die einzige Brücke zum Werte-Kasten.

    Ohne die Beträge darin steht der Kasten leer da: im Browser wird nichts
    gerechnet, dort wird nur zugeordnet.
    """
    bild = _bild([_punkt(1, {"Abo": "60.00", "Rabatt": "-4.00"}, "56.00")])
    daten = bild.als_json()
    assert len(daten) == 1
    zeilen = daten[0]["zeilen"]
    assert sorted(zeilen.values()) == ["56.00", "60.00", "−4.00"]
    # Die Schlüssel sind dieselben wie im Markup der Vorlage.
    assert set(zeilen) - {BEZAHLT} <= {p.schluessel for p in bild.zeilen_vorlage}


# ---------------------------------------------------------------------------
# Steht die Geometrie auch im Markup?
# ---------------------------------------------------------------------------
#
# Der Dienst rechnet, das Template zeichnet. Nachgemessen liess sich die halbe
# Aussage im Template löschen, ohne dass ein Test rot wurde: Bandhöhe fest auf
# 100 %, Stapelhöhe fest auf 100 %, Rabattzeile weg, Legende weg, Werte-Kasten
# ohne Positionszeilen. Alles grün, und auf der Seite stand ein Bild, das etwas
# anderes behauptet.

# Erfundene Reihe mit allem, was im Bild vorkommen kann: laufende Monate, ein
# einmaliger Grossposten samt Rückgaberabatt, ein absichtlich zu langer
# Positionsname.
LANGER_NAME = "Sehr lange Bezeichnung einer erfundenen Position ohne Umbruch"
LAUFEND = {"Grundgebuehr Erfunden": "80.00", LANGER_NAME: "15.00",
           "Aktionsrabatt": "-5.00"}
AUSREISSER = {"Grundgebuehr Erfunden": "80.00", LANGER_NAME: "15.00",
              "Erfundenes Faltgeraet": "1200.00", "Rabatt Altgeraet": "-200.00"}


@pytest.fixture
def gebauter_ausreisser():
    """Sechs laufende Monate und einer mit einem Gerätekauf — und räumt auf."""
    marke = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        reihe = MetricSeries(
            slug=f"kappung_{marke}", name=f"Erfundene Rechnung {marke}",
            unit=MetricUnit.CHF, cadence=MetricCadence.MONATLICH,
            kind=MetricKind.AUSGABE,
        )
        db.add(reihe)
        db.flush()
        for monat in range(1, 8):
            pos = AUSREISSER if monat == 7 else LAUFEND
            db.add(MetricPoint(
                series_id=reihe.id, period_start=date(JAHR, monat, 1),
                period_end=date(JAHR, monat, 28),
                value=Decimal("1095.00" if monat == 7 else "90.00"),
                source="erfundener-beleg.pdf",
                extras={f"pos:{k}": v for k, v in pos.items()},
            ))
        db.commit()
        reihe_id, slug = reihe.id, reihe.slug

    yield slug

    with SessionLocal() as db:
        if (obj := db.get(MetricSeries, reihe_id)) is not None:
            db.delete(obj)
        db.commit()


def _bild_aus_db(slug: str):
    with SessionLocal() as db:
        r = db.scalar(select(MetricSeries).where(MetricSeries.slug == slug))
        return positions_bild(verlauf(db, r))


def test_die_achse_richtet_sich_nach_den_laufenden_monaten(gebauter_ausreisser) -> None:
    """Feste Erwartung an einer festen Reihe: die Achse trägt sechs Monate mit
    95.00 brutto und einen mit 1295.00. Deckel ist der Median mal 1.5, auf zwei
    geltende Stellen aufgerundet — 150.00.

    Ohne Deckel bekäme ein laufender Monat 7 % der Zeilenhöhe; mit Deckel sind
    es 63 %, und die Monate sind wieder untereinander vergleichbar.
    """
    bild = _bild_aus_db(gebauter_ausreisser)
    assert bild.deckel == Decimal("150")
    assert bild.balken[0].h_oben == pytest.approx(63.33, abs=0.01)
    assert bild.balken[-1].h_oben == pytest.approx(100.0)


def test_jedes_band_traegt_seine_hoehe_im_markup(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Mit fester Bandhöhe zeigte der Balken lauter gleich hohe Positionen."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    anteile = [s.anteil for s in bild.balken[0].oben]
    assert len(set(anteile)) > 1, "Vorbedingung: die Bänder sind verschieden hoch"
    for anteil in anteile:
        assert f"height:{anteil}%" in karte, f"Band {anteil}% fehlt im Markup"


def test_jeder_stapel_traegt_die_hoehe_seiner_periode(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Ohne sie hat jeder Monat die volle Höhe, und der Verlauf ist weg."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    hoehe = bild.balken[0].h_oben
    assert hoehe < 100.0, "Vorbedingung: der laufende Monat füllt die Zeile nicht"
    assert f'class="vb-stapel" style="height:{hoehe}%"' in " ".join(karte.split())


def test_die_rabattzeile_steht_unter_der_nulllinie_im_markup(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Fiel sie weg, verschwanden die Rabatte spurlos — der Balken zeigte dann
    Bruttokosten und die Linie einen kleineren bezahlten Betrag."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    assert "vb-feld-u" in karte
    tief = bild.balken[0].h_unten
    assert tief > 0
    assert f"height:{tief}%" in " ".join(karte.split())


def test_der_werte_kasten_traegt_eine_zeile_je_position(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Ohne die Zeilen im Markup bliebe der Kasten leer: das Skript baut keinen
    Positionsnamen zusammen — er käme sonst als HTML aus einer PDF in die Seite.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    zeilen = re.findall(r'class="vb-tip-zeile" data-pos="([^"]*)"', karte)
    assert zeilen == [p.schluessel for p in bild.zeilen_vorlage]
    for p in bild.zeilen_vorlage:
        assert p.name in karte


def test_die_legende_steht_unter_dem_bild(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Ohne sie ist keine Farbe einem Namen zuzuordnen — und genau das ist die
    Frage, die das Diagramm beantworten soll. Sie steht zugeklappt (siehe
    ``test_die_legende_steht_zugeklappt_hinter_einem_griff``), aber vollständig
    im Markup: nachgeladen wird nichts."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    legende = re.search(r'class="vl-legende vb-legende">(.*?)</div>', karte, re.S)
    assert legende, "Keine Legende im Markup"
    for p in bild.posten:
        assert p.name in legende.group(1)
    assert "bezahlt" in legende.group(1)


def test_die_legende_steht_offen_hinter_einem_griff(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Die Legende steht OFFEN — und trotzdem hinter einem Griff.

    Sie war zugeklappt, weil sie bei 375px acht Zeilen über 182px hoch ist,
    höher als das Zeichenfeld darüber (172px). Die Messung stimmt, die Abwägung
    war falsch: der Werte-Kasten beantwortet dieselbe Frage erst, wenn man einen
    Balken TRIFFT. Wer den Chart überfliegt, will die Zuordnung sofort sehen.
    Sie war ausdrücklich zusätzlich zum Hover gewünscht.

    Der Griff bleibt, samt Zahl: zuklappen muss möglich sein, und ohne die Zahl
    wäre er eine blosse Aufschrift statt einer Information.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    bild = _bild_aus_db(gebauter_ausreisser)
    griff = re.search(
        r'<details class="([^"]*vb-legende-box[^"]*)"([^>]*)>\s*<summary>(.*?)</summary>',
        karte, re.S,
    )
    assert griff, "Die Legende steht nicht hinter einem Aufklapper"
    assert "open" in griff.group(2), "Der Aufklapper steht zugeklappt"
    assert str(len(bild.posten)) in griff.group(3), (
        f"Der Griff nennt die Zahl der Posten nicht: {griff.group(3)!r}"
    )
    # Der Aufklapper umschliesst die Legende und nicht bloss irgendetwas.
    assert karte.index('class="vl-legende vb-legende"') > karte.index(griff.group(0))


def test_die_schnittmarke_und_ihre_zeile_stehen_im_markup(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Ein Balken, der bloss an der Decke endet, wäre eine Lüge über seine Höhe.

    Also beides: die aufgezackte Kante am Balken und darunter die Stelle, die
    benennt, was oberhalb liegt — mit Namen und Betrag.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    assert "is-gekappt" in karte, "Der gekappte Balken trägt keine Schnittmarke"
    text = " ".join(_block(karte, "vb-kappung", "details").split())
    assert "Erfundenes Faltgeraet" in text
    assert "1&#39;200.00" in text or "1'200.00" in text
    assert "Rabatt Altgeraet" in text


def test_die_kappung_steht_als_aufstellung_und_nicht_als_satz(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Als Fliesstext lief sie bei 375px über drei Zeilen, und die halbe Länge
    waren „über der Achse:" und „unter der Achse:" — Wendungen, die weder einen
    Posten noch einen Betrag benennen. Die Richtung steht am Vorzeichen: ein
    Rabatt trägt sein echtes Minus.

    Geprüft wird die FORM, weil nur sie sich still verlieren kann: eine Zeile je
    Posten, der Betrag in einer eigenen Zelle. Ohne diese Trennung stünde wieder
    ein Satz da, in dem Name und Zahl ineinanderlaufen.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    block = _block(karte, "vb-kappung", "details")
    bild = _bild_aus_db(gebauter_ausreisser)

    for wendung in ("über der Achse", "unter der Achse"):
        assert wendung not in block, f"Konstruktionswort wieder da: {wendung}"

    # Das Schweizer Hochkomma steht im Markup escaped — verglichen wird darum
    # mit derselben Escape-Funktion, die Jinja beim Rendern nimmt.
    erwartet = 0
    for k in bild.kappungen:
        assert k.label in block, f"Periode {k.label} fehlt"
        for name, betrag in [*k.posten, *k.posten_unten]:
            assert f'<span class="vb-kappung-name">{escape(name)}</span>' in block
            assert f'<span class="mono">{escape(betrag)}</span>' in block
            erwartet += 1
        if k.bezahlt:
            erwartet += 1
    assert erwartet > 1, "Vorbedingung: die Aufstellung hat mehrere Posten"
    assert block.count('class="vb-kappung-zeile') == erwartet, (
        "Eine Zeile je Posten — nicht eine Zeile je Periode"
    )


def test_das_abgeschnitten_steht_bei_der_kappung_und_nicht_in_der_legende(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Die Legende erklärt Bänder des Bildes. Die Zackenmarke gehört zu einer
    Stelle, die es nur bei gekappten Perioden gibt — dort steht sie jetzt, mit
    dem Wort daneben. Zweimal dasselbe Zeichen erklärt kostete eine Legendenzeile
    und liess offen, wohin es zeigt.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    legende = re.search(r'class="vl-legende vb-legende">(.*?)</div>', karte, re.S)
    assert legende
    assert "abgeschnitten" not in legende.group(1)
    block = _block(karte, "vb-kappung", "details")
    assert "vb-key-schnitt" in block and "abgeschnitten" in block


def test_der_lange_positionsname_ist_im_kasten_nachschlagbar(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Im Kasten waren von 415 px Name 188 px zu lesen, der Rest Ellipse.

    Der Name darf jetzt umbrechen (CSS) und trägt zusätzlich ein ``title`` —
    ohne beides ist er am Handy dort nicht zu erfahren.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    assert f'class="vb-tip-name" title="{LANGER_NAME}"' in karte
    assert _regel(".vb-tip-name").get("overflow-wrap") == "anywhere"
    assert "nowrap" not in _regel(".vb-tip-name").get("white-space", "")


# ---------------------------------------------------------------------------
# Was das Stylesheet tragen MUSS
# ---------------------------------------------------------------------------
#
# Auch hier: nachgemessen liess sich jede dieser Regeln ändern, ohne dass ein
# Test rot wurde — Spalt ins Raster, Mindesthöhe auf null, Stapelrichtung
# gedreht, Balkenbreite auf 5 %.


def test_das_spaltenraster_hat_keinen_spalt() -> None:
    """Nur bei lückenlosen Spalten liegt die Mitte der i-ten Spalte bei
    (i+0.5)/n — und genau dort setzt der Dienst die Punkte der Bezahlt-Linie.
    Mit Spalt wandern Balken und Linienpunkt auseinander, am stärksten an den
    Rändern. Der Abstand zwischen den Balken kommt aus deren BREITE."""
    plot = _regel(".vb-plot")
    assert not [k for k in plot if k.endswith("gap")], plot


def test_der_balken_laesst_platz_in_seiner_spalte() -> None:
    """Zu breit: kein Abstand zum Nachbarn. Zu schmal: der Monat ist ein Strich."""
    breite = _regel(".vb-chart").get("--vb-breite", "")
    assert breite.endswith("%")
    assert 40 <= float(breite.rstrip("%")) <= 80, breite


def test_die_stapel_wachsen_von_der_nulllinie_weg() -> None:
    """Oben von unten nach oben, unten von oben nach unten. Gedreht passt die
    Legende nicht mehr zum Stapel: das erste Muster stünde am falschen Ende."""
    assert _regel(".vb-stapel")["flex-direction"] == "column-reverse"
    assert _regel(".vb-stapel-u")["flex-direction"] == "column"


def test_ein_winziger_stapel_behaelt_eine_mindesthoehe() -> None:
    """Ohne sie bliebe eine Periode mit sehr kleinem Betrag ein unsichtbarer
    Strich und sähe aus wie „nichts erfasst"."""
    wert = _regel(".vb-stapel:not(:empty)")["min-height"]
    assert wert.endswith("px") and float(wert.rstrip("px")) >= 3


def test_die_trennlinie_frisst_das_schmale_band_nicht() -> None:
    """Innen liegend war die Linie bei schmalen Bändern das ganze Band: 14 von
    174 Bändern unter 1 px, gezeichnet in Kartenfarbe — eine Lücke im Balken."""
    assert not _regel(".vb-seg")["box-shadow"].startswith("inset")


def test_das_linienfeld_endet_an_der_nulllinie() -> None:
    """Es trägt die Bezahlt-Linie, deren y in Prozent der OBEREN Zeile gerechnet
    ist. Über die ganze Fläche gespannt, löste sich die Linie von den Balken."""
    assert "--vb-oben" in _regel(".vb-linienfeld")["height"]


def test_die_achsenzahlen_halten_abstand_voneinander() -> None:
    """Die Rabattmarke sitzt auf der Tiefe, die sie benennt. Schrumpft ihre
    Zeile, landete sie auf derselben Grundlinie wie die „0" — gemessen drei
    Zahlen ineinander. Die Klemme bei 1.15em hält sie auseinander."""
    assert "1.15em" in _regel(".vb-yachse-u > span")["top"]


def test_der_unsicher_rahmen_bleibt_in_seiner_spalte() -> None:
    """Gemessen bei 375px und 24 Perioden: Spalte 11.4 px, Balken 6.8 px, also
    4.6 px Spalt. Umriss und Versatz zehren davon auf jeder Seite; bei 2+2 px
    blieben 0.6 px Luft zum Nachbarbalken."""
    regel = _regel(".vb-stapel.is-unsicher")
    versatz = float(regel["outline-offset"].rstrip("px"))
    breite = float(regel["outline"].split()[0].rstrip("px"))
    assert versatz + breite <= 2, f"{versatz} + {breite} px frisst den Spalt"


def test_der_werte_kasten_verdeckt_achse_und_legende_nicht() -> None:
    """Der Kasten ist höher als das Zeichenfeld — 309 px gegen 148 px bei 375px
    — und lag auf Achse und Legende; deren Reste lugten daneben hervor. Solange
    gelesen wird, treten sie zurück; `visibility` hält den Platz, damit die
    Karte nicht springt."""
    for teil in (".vb-legende", ".vb-xachse"):
        assert _regel(f".vl-karte.vb-liest {teil}")["visibility"] == "hidden"


# ---------------------------------------------------------------------------
# Was das Skript tragen MUSS
# ---------------------------------------------------------------------------


def test_das_skript_zeigt_die_periode_unter_dem_zeiger() -> None:
    """Ohne die Suche nach dem nächsten Punkt zeigte der Kasten immer die erste
    Periode — dieselben Zahlen, egal wohin man fasst."""
    rumpf = _js_funktion("initPositionsBalken")
    assert re.search(r"if \(d < naechster\) \{\s*naechster = d;\s*treffer = i;", rumpf)


def test_das_skript_blendet_zeilen_ohne_wert_aus() -> None:
    """Eine Position, die es in dieser Periode nicht gibt, hat keine Zeile.

    Bliebe sie stehen, behauptete der Kasten eine Position, die auf dieser
    Rechnung nicht steht.
    """
    rumpf = _js_funktion("initPositionsBalken")
    assert "z.hidden = wert === undefined;" in rumpf


def test_das_skript_raeumt_legende_und_achse_weg_solange_gelesen_wird() -> None:
    """Die Zeile, die im Vorbild steht und hier fehlte — Befund bei JEDER
    Berührung, nicht am Randfall."""
    rumpf = _js_funktion("initPositionsBalken")
    assert 'karte.classList.add("vb-liest")' in rumpf
    assert 'karte.classList.remove("vb-liest")' in rumpf


def test_die_kappung_steht_zugeklappt_und_nennt_den_monat(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Die Aufstellung des gekappten Monats ist ein Aufklapper, kein Dauertext.

    Gemessen bei 375 px: offen ist sie 120 px hoch und stand zwischen Bild und
    Legende — die Karte kam auf 799 px, wovon 172 px das Diagramm waren.
    Zugeklappt sind es 17 px und die Karte 696 px. Dass gekappt wurde, sagt
    schon die Zackenkante am Balken; die Aufstellung beantwortet die Frage
    danach, und die stellt man erst, wenn man den Zacken sieht.

    Der MONAT gehört in die Zusammenfassung: ohne ihn müsste man aufklappen,
    nur um zu wissen, ob einen der Ausreisser überhaupt betrifft.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    assert '<details class="disclosure vb-kappung">' in karte, \
        "Die Kappung steht wieder dauerhaft offen"
    kopf = karte[karte.index("vb-kappung-kopf"):]
    kopf = kopf[:kopf.index("</summary>")]
    assert "abgeschnitten" in kopf
    assert f"07.{JAHR}" in kopf, \
        f"Der gekappte Monat fehlt in der Zusammenfassung: {kopf!r}"


def test_die_kappung_verliert_beim_zuklappen_keine_zeile(
    logged_in_client: TestClient, gebauter_ausreisser
) -> None:
    """Die Gegenprobe: zugeklappt heisst NICHT weniger Inhalt.

    Der Nutzer will genau diese Aufschlüsselung — Gerätename, Originalpreis und
    jeden Rabatt einzeln. Ein Aufklapper, der dabei etwas weglässt, wäre
    schlimmer als der Dauertext, den er ersetzt.
    """
    karte = _karte(logged_in_client.get("/verlaeufe").text, gebauter_ausreisser)
    block = _block(karte, "vb-kappung", "details")
    for name in ("Erfundenes Faltgeraet", "Rabatt Altgeraet"):
        assert name in block, f"{name} fehlt in der Aufstellung"
    assert "1&#39;200.00" in block, "Der Originalpreis fehlt"
    assert "−200.00" in block, "Der Rabatt fehlt"
