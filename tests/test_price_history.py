"""Preisverlauf aus Belegpositionen.

Alle Belege hier sind erfundene Testdaten — Beträge und Artikelnamen sind im
Test selbst nachlesbar. Ein Stichjahr ohne andere Testdaten hält die Auswertung
vom gemeinsamen Bestand fern.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal

import pytest

from moneten.db.models import PendingReceipt
from moneten.db.session import SessionLocal
from moneten.services.price_history import artikel_schluessel, preisverlauf


def _beleg(db, tag: date, haendler: str, positionen: list[tuple[str, str]]) -> PendingReceipt:
    p = PendingReceipt(
        merchant=haendler,
        receipt_date=tag,
        amount=Decimal("0"),
        items_json=json.dumps({
            "merchant": haendler,
            "items": [{"name": n, "price": pr} for n, pr in positionen],
        }),
        source="photo",
    )
    db.add(p)
    return p


def _verlauf_fuer(db, name_teil: str):
    return [a for a in preisverlauf(db) if name_teil.lower() in a.name.lower()]


# ---------------------------------------------------------------- Schlüssel


def test_wortreihenfolge_egal() -> None:
    assert artikel_schluessel("Bio Butter") == artikel_schluessel("Butter Bio")


def test_gebindegroesse_trennt_artikel() -> None:
    """„Butter 250g" und „Butter 500g" sind zwei Produkte, kein Preissprung."""
    assert artikel_schluessel("Butter 250g") != artikel_schluessel("Butter 500g")


def test_schreibweise_der_gebindegroesse_egal() -> None:
    assert artikel_schluessel("Butter 250 g") == artikel_schluessel("Butter 250g")


def test_stueckzahl_gehoert_nicht_zum_artikel() -> None:
    assert artikel_schluessel("2 x Butter 250g") == artikel_schluessel("Butter 250g")


# ---------------------------------------------------------------- Verlauf


def test_zwei_belege_ergeben_einen_verlauf() -> None:
    tag = uuid.uuid4().hex[:6]
    name = f"ZZZbutter{tag} 250g"
    with SessionLocal() as db:
        b1 = _beleg(db, date(2015, 3, 4), "Migros", [(name, "3.20")])
        b2 = _beleg(db, date(2015, 6, 4), "Migros", [(name, "3.80")])
        db.commit()

        treffer = _verlauf_fuer(db, f"ZZZbutter{tag}")
        assert len(treffer) == 1
        a = treffer[0]
        assert a.erst == Decimal("3.20")
        assert a.letzt == Decimal("3.80")
        assert a.diff == Decimal("0.60")
        assert round(a.pct) == 19

        db.delete(b1)
        db.delete(b2)
        db.commit()


def test_ein_einzelner_beleg_ist_kein_verlauf() -> None:
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        b = _beleg(db, date(2015, 3, 4), "Migros", [(f"ZZZeinmal{tag}", "3.20")])
        db.commit()
        assert _verlauf_fuer(db, f"ZZZeinmal{tag}") == []
        db.delete(b)
        db.commit()


def test_zwei_positionen_am_selben_tag_sind_kein_verlauf() -> None:
    """Zwei Zeilen auf einem Beleg zeigen keine Preisentwicklung."""
    tag = uuid.uuid4().hex[:6]
    name = f"ZZZdoppel{tag}"
    with SessionLocal() as db:
        b = _beleg(db, date(2015, 3, 4), "Migros", [(name, "3.20"), (name, "3.20")])
        db.commit()
        assert _verlauf_fuer(db, name) == []
        db.delete(b)
        db.commit()


def test_stueckzahl_wird_zum_stueckpreis_gerechnet() -> None:
    """Sonst sähe jeder Vorratseinkauf wie eine Preiserhöhung aus."""
    tag = uuid.uuid4().hex[:6]
    basis = f"ZZZvorrat{tag} 250g"
    with SessionLocal() as db:
        b1 = _beleg(db, date(2015, 3, 4), "Migros", [(basis, "3.20")])
        b2 = _beleg(db, date(2015, 6, 4), "Migros", [(f"2 x {basis}", "6.40")])
        db.commit()

        treffer = _verlauf_fuer(db, f"ZZZvorrat{tag}")
        assert len(treffer) == 1, "Stückzahl darf keinen zweiten Artikel erzeugen"
        assert treffer[0].letzt == Decimal("3.20")
        assert treffer[0].diff == Decimal("0")

        db.delete(b1)
        db.delete(b2)
        db.commit()


def test_guenstigster_punkt_wird_gefunden() -> None:
    tag = uuid.uuid4().hex[:6]
    name = f"ZZZkaffee{tag}"
    with SessionLocal() as db:
        belege = [
            _beleg(db, date(2015, 1, 5), "Coop", [(name, "8.90")]),
            _beleg(db, date(2015, 2, 5), "Denner", [(name, "6.50")]),
            _beleg(db, date(2015, 3, 5), "Coop", [(name, "9.20")]),
        ]
        db.commit()
        a = _verlauf_fuer(db, name)[0]
        assert a.guenstigster.preis == Decimal("6.50")
        assert a.guenstigster.haendler == "Denner"
        assert a.punkte[0].datum < a.punkte[-1].datum, "Punkte müssen chronologisch stehen"
        for b in belege:
            db.delete(b)
        db.commit()


def test_teuerste_entwicklung_steht_oben() -> None:
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        belege = [
            _beleg(db, date(2015, 1, 5), "Coop",
                   [(f"ZZZruhig{tag}", "10.00"), (f"ZZZsteil{tag}", "2.00")]),
            _beleg(db, date(2015, 4, 5), "Coop",
                   [(f"ZZZruhig{tag}", "10.50"), (f"ZZZsteil{tag}", "4.00")]),
        ]
        db.commit()
        namen = [a.name for a in preisverlauf(db) if "ZZZ" in a.name and tag in a.name]
        assert namen[0] == f"ZZZsteil{tag}", f"Reihenfolge falsch: {namen}"
        for b in belege:
            db.delete(b)
        db.commit()


# ---------------------------------------------------------------- Seite


def test_seite_laedt_auch_ohne_belege(logged_in_client) -> None:
    """Ohne Quittungen zeigt die Seite eine Erklärung, keinen leeren Rumpf."""
    resp = logged_in_client.get("/preise")
    assert resp.status_code == 200
    assert "Preisverlauf" in resp.text


def test_seite_zeigt_verlauf(logged_in_client) -> None:
    tag = uuid.uuid4().hex[:6]
    name = f"ZZZseite{tag}"
    with SessionLocal() as db:
        b1 = _beleg(db, date(2015, 3, 4), "Migros", [(name, "3.20")])
        b2 = _beleg(db, date(2015, 6, 4), "Migros", [(name, "4.00")])
        db.commit()
        ids = (b1.id, b2.id)

    resp = logged_in_client.get("/preise")
    assert name in resp.text
    assert "+25%" in resp.text

    with SessionLocal() as db:
        for i in ids:
            db.delete(db.get(PendingReceipt, i))
        db.commit()


# ---------------------------------------------------------------- Ende zu Ende


def _beleg_aus_text(db, tag: date, haendler: str, belegtext: str) -> PendingReceipt:
    """Beleg über den ECHTEN Parser, nicht mit handgebauten Positionen.

    Der erste Anlauf dieser Tests baute die Positionen direkt zusammen und war
    deshalb grün, obwohl die Stückzahl-Erkennung gar nicht funktionierte: der
    Beleg-Parser entfernt die Menge aus dem Namen, bevor sie gespeichert wird.
    Der Test prüfte also eine Eingabe, die in der App nie vorkommt.
    """
    from moneten.services.receipt_split import parse_receipt_items_menge

    positionen = [
        {"name": n, "price": str(pr), "qty": q}
        for n, pr, q in parse_receipt_items_menge(belegtext)
    ]
    p = PendingReceipt(
        merchant=haendler, receipt_date=tag, amount=Decimal("0"),
        items_json=json.dumps({"merchant": haendler, "items": positionen}),
        source="photo",
    )
    db.add(p)
    return p


def test_multipack_erzeugt_keinen_preissprung() -> None:
    """Regressionstest: „2 x Butter 11.80" muss als 5.90 pro Stück ankommen.

    Vorher lief die Stückzahl ins Leere — der Verlauf zeigte 5.90 → 11.80 und
    behauptete +100 % Teuerung, wo nur zwei Päckchen gekauft wurden.
    """
    tag = uuid.uuid4().hex[:6]
    art = f"ZZZbtr{tag} Bio 250g"
    with SessionLocal() as db:
        b1 = _beleg_aus_text(db, date(2015, 1, 10), "Migros", f"{art}          5.90")
        b2 = _beleg_aus_text(db, date(2015, 3, 10), "Migros", f"2 x {art}     11.80")
        db.commit()

        treffer = _verlauf_fuer(db, f"ZZZbtr{tag}")
        assert len(treffer) == 1, f"ein Artikel erwartet, nicht {[a.name for a in treffer]}"
        a = treffer[0]
        assert [p.preis for p in a.punkte] == [Decimal("5.90"), Decimal("5.90")]
        assert a.diff == Decimal("0"), "Vorratseinkauf ist keine Preiserhöhung"

        db.delete(b1)
        db.delete(b2)
        db.commit()


def test_stueckzahl_mit_st_schreibweise() -> None:
    """„2 ST Butter" ist dieselbe Aussage wie „2 x Butter"."""
    from moneten.services.receipt_split import stueckzahl_aus_zeile

    assert stueckzahl_aus_zeile("2 x Butter Bio 250g") == 2
    assert stueckzahl_aus_zeile("2 ST Butter Bio 250g") == 2
    assert stueckzahl_aus_zeile("Gipfeli 3 Stk") == 3
    assert stueckzahl_aus_zeile("Butter Bio 250g") == 1


def test_stangen_sind_keine_stueckzahl() -> None:
    """Ohne Wortgrenze läse die Regex aus „2 Stangen Brot" ein „2 St"."""
    from moneten.services.receipt_split import stueckzahl_aus_zeile

    assert stueckzahl_aus_zeile("2 Stangen Brot") == 1
    assert stueckzahl_aus_zeile("1 Stollen") == 1


def test_gewicht_wird_nicht_geteilt() -> None:
    """„0.5 kg Rüebli" ist ein Gewicht — der Preis gilt für die ganze Packung."""
    from moneten.services.receipt_split import stueckzahl_aus_zeile

    assert stueckzahl_aus_zeile("0.5 kg Rüebli") == 1
    assert stueckzahl_aus_zeile("500 g Mehl") == 1


def test_zaehlung_beruecksichtigt_auch_gekappte_artikel(logged_in_client) -> None:
    """Regressionstest: erst zählen, dann kappen.

    Sortiert wird nach Preisänderung absteigend, abgeschnitten also das untere
    Ende — genau die stärksten Verbilligungen. Auf der gekappten Liste gezählt
    meldete die Karte „0 günstiger", obwohl vieles billiger geworden war.
    """
    from moneten.routers.prices import _MAX_ARTIKEL

    marke = uuid.uuid4().hex[:4]
    n_teurer = _MAX_ARTIKEL + 5
    n_guenstiger = 3
    with SessionLocal() as db:
        frueher, spaeter = [], []
        for i in range(n_teurer):
            frueher.append((f"ZZZteuer{marke}nr{i}", "10.00"))
            spaeter.append((f"ZZZteuer{marke}nr{i}", "20.00"))
        for i in range(n_guenstiger):
            frueher.append((f"ZZZbillig{marke}nr{i}", "10.00"))
            spaeter.append((f"ZZZbillig{marke}nr{i}", "5.00"))
        b1 = _beleg(db, date(2014, 1, 5), "Coop", frueher)
        b2 = _beleg(db, date(2014, 4, 5), "Coop", spaeter)
        db.commit()
        ids = (b1.id, b2.id)

    resp = logged_in_client.get("/preise")
    assert resp.status_code == 200
    assert f'<strong class="mono">{n_guenstiger}</strong> günstiger' in resp.text, (
        "Die günstiger gewordenen Artikel wurden auf der gekappten Liste gezählt"
    )

    with SessionLocal() as db:
        for i in ids:
            db.delete(db.get(PendingReceipt, i))
        db.commit()


# ------------------------------------------------------ Was die Seite ZEIGT
#
# Die Seite wird am Handy gelesen. Beide Prüfungen hier hängen an der FORM, und
# Form verliert sich still: ein Satz mehr unter der Liste, ein Erklärkasten
# wieder aufgeklappt — nichts davon macht irgendetwas rot.


@pytest.fixture
def zwei_belege():
    """Ein Artikel, der teurer wurde: der tiefste Preis liegt in der Vergangenheit."""
    marke = uuid.uuid4().hex[:4]
    name = f"ZZZmarke{marke} Erfundene Butter 250g"
    with SessionLocal() as db:
        b1 = _beleg(db, date(2013, 2, 4), "Erfundener Laden", [(name, "3.20")])
        b2 = _beleg(db, date(2013, 6, 9), "Erfundener Laden", [(name, "3.95")])
        db.commit()
        ids = (b1.id, b2.id)

    yield marke

    with SessionLocal() as db:
        for i in ids:
            if (obj := db.get(PendingReceipt, i)) is not None:
                db.delete(obj)
        db.commit()


def test_der_tiefste_preis_wird_markiert_statt_beschrieben(
    logged_in_client, zwei_belege
) -> None:
    """Unter der Liste stand „Am günstigsten war er am … für … bei …" — Datum,
    Betrag und Händler einer Zeile, die drei Zeilen weiter oben schon steht. Der
    Satz zeigte auf eine Zeile, statt sie zu markieren.

    Geprüft wird beides: dass die Marke an der richtigen Zeile hängt und dass der
    Satz nicht zurückkommt. Ohne die zweite Hälfte stünden irgendwann beide da.
    """
    html = logged_in_client.get("/preise").text
    assert "Am günstigsten war er am" not in html, "Der Satz ist wieder da"

    # Die Marke sitzt in der Zeile mit dem tiefsten Preis, nicht irgendwo.
    zeile = re.search(r'<div class="row">\s*<span>04\.02\.2013(.*?)</div>', html, re.S)
    assert zeile, "Die Zeile des tiefsten Preises fehlt"
    assert 'class="pv-tief"' in zeile.group(1), "Die Zeile trägt keine Marke"
    assert "3.20" in zeile.group(1)
    # Und NUR dort: die jüngere, teurere Beobachtung ist kein Fund.
    spaeter = re.search(r'<div class="row">\s*<span>09\.06\.2013(.*?)</div>', html, re.S)
    assert spaeter and 'class="pv-tief"' not in spaeter.group(1)


def test_der_tiefste_preis_bleibt_unmarkiert_wenn_er_der_aktuelle_ist(
    logged_in_client,
) -> None:
    """Sonst hinge die Marke „günstigster" am heutigen Preis und läse sich als
    vergangener Fund. Dieselbe Bedingung, unter der vorher der Satz stand."""
    marke = uuid.uuid4().hex[:4]
    name = f"ZZZfallend{marke} Erfundener Kaffee 500g"
    with SessionLocal() as db:
        b1 = _beleg(db, date(2012, 3, 6), "Erfundener Laden", [(name, "9.50")])
        b2 = _beleg(db, date(2012, 8, 14), "Erfundener Laden", [(name, "7.95")])
        db.commit()
        ids = (b1.id, b2.id)

    html = logged_in_client.get("/preise").text
    block = html[html.index(f"ZZZfallend{marke}"):]
    block = block[:block.index("</details>")]
    assert 'class="pv-tief"' not in block, (
        "Der aktuelle Preis ist als vergangener Fund markiert"
    )

    with SessionLocal() as db:
        for i in ids:
            if (obj := db.get(PendingReceipt, i)) is not None:
                db.delete(obj)
        db.commit()


def test_die_zuordnungs_erklaerung_steht_zugeklappt(logged_in_client) -> None:
    """Hier stand der längste Satz der App (17 Wörter über die Gebindegrösse),
    zusammen mit drei weiteren, am Fuss JEDES Aufrufs. Es ist eine Erklärung, die
    man einmal liest — der Text bleibt vollständig, aber hinter einem Griff.
    """
    html = logged_in_client.get("/preise").text
    kasten = re.search(
        r'<details class="([^"]*pv-erklaerung[^"]*)"([^>]*)>(.*?)</details>', html, re.S,
    )
    assert kasten, "Die Erklärung steht nicht in einem Aufklapper"
    assert "open" not in kasten.group(2), "Der Aufklapper steht offen"
    assert "<summary>" in kasten.group(3), "Kein Griff zum Aufklappen"
    # Vollständig: die Aussage über die Gebindegrösse ist der Kern der Erklärung.
    assert "Gebindegrösse" in kasten.group(3)
    assert "Stückpreis" in kasten.group(3)
    assert "Datum bei jedem Preis" in kasten.group(3)
