"""Beispielfunk-Rechnung: Spaltentext, Positionen, Selbstprüfung, Import.

**Jeder Text, jeder Betrag und jeder Name in dieser Datei ist erfunden.** Aus dem
Bestand stammt allein das *Layout* — die Abschnittsnamen der Rechnung, die
Spaltenüberschriften, die Stelle, an der ein Zeitraum steht. Die Produktnamen
sind Katalogbezeichnungen und stehen so in jedem Prospekt. Die Zahlen sind
runde Erfindungen, an denen sich die Summe im Kopf nachrechnen lässt.

Der Aufbau folgt dem Datenweg: erst der Spaltentext aus dem PDF
(``scripts/verlaeufe_aus_scans.py``), dann die Deutung
(``services/belege_parser.py``), dann die Aufnahme in die App
(``routers/metrics.py``).
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Category,
    MetricCadence,
    MetricKind,
    MetricPoint,
    MetricSeries,
    MetricUnit,
)
from moneten.db.session import SessionLocal
from moneten.services.belege_parser import (
    POS_PRAEFIX,
    POS_RABATT,
    POS_RUNDUNG,
    PROFILE,
    PruefsummeFehler,
    rechnung_nach_profil,
)

WURZEL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WURZEL / "scripts"))

import verlaeufe_aus_scans as skript  # noqa: E402

# Die Tests laufen am MITGELIEFERTEN Beispielprofil, nicht an einem echten
# Anbieter: sonst stuende dessen Rechnungswortlaut im oeffentlichen Teil.
PROFIL = PROFILE["beispielfunk"]


def _lies(text: str):
    """Kurz fuer `rechnung_nach_profil(text, PROFIL)`."""
    return rechnung_nach_profil(text, PROFIL)

# ---------------------------------------------------------------------------
# Erfundene Rechnung im Spaltenformat
# ---------------------------------------------------------------------------
#
# Zwei Verträge, ein Auslandsblock, eine Rundungsdifferenz. Die Positionen:
#   80.00 + 10.00 - 20.00 + 4.00 + 30.00 - 4.00 = 100.00
#   100.00 + (-0.05) = 99.95 = Rechnungsbetrag
#
# Die „Kostenübersicht" oben nennt bewusst ANDERE Beträge und einen Posten
# („blue TV Irgendwas"), den es unten gar nicht gibt: würde sie mitgelesen,
# fiele es sofort auf.
_RECHNUNG = "\n".join([
    "Rechnung",
    "Rechnungsbetrag\tCHF 99.95",
    "Kundennummer\t10000001",
    "Rechnung für März 2031",
    "Position\tBetrag",
    "Grundgebühren\t555.00",
    "Rechnungstotal inkl. MWST\t99.95",
    "Kostenübersicht",
    "Grundgebühren\t555.00\tVerbrauch\t444.00",
    "blue TV Irgendwas\t333.00",
    "Positionen im Detail",
    "079 000 00 00\tVertragsreferenz: 10000001-20000002",
    "Adresse",
    "Ariadne Muster Musterweg 1",
    "9999 Musterhausen",
    "Menge\tPreis pro Einheit\tBetrag",
    "Grundgebühren\t80.00",
    "blue Mobile M\t1\t80.00\t80.00",
    "Zusatzleistungen",
    "Connect Pack\t1\t10.00\t10.00",
    "Zusatzleistungen",
    "blue Benefit: Kombi-Rabatt\t1\t-20.00\t-20.00",
    "Verbrauch\t4.00",
    "Im Ausland abgehend",
    "Telefonie, Spezialtarif\t4.00",
    "Summe\t74.00",
    "Aufstellung nach Ziel im Ausland\tDienst\tLand\tBetrag",
    "Telefonie\tIrgendwoland\t4.00",
    "Summe\t4.00",
    "Vertrag 30000000000003",
    "Adresse",
    "Ariadne Muster Musterweg 1",
    "9999 Musterhausen",
    "Menge\tPreis pro Einheit\tBetrag",
    "Grundgebühren\t30.00",
    "blue home: blue Internet M\t1\t30.00\t30.00",
    "Zusatzleistungen",
    "Promotion Internet M (3/12)\t1\t-4.00\t-4.00",
    "Summe\t26.00",
    "Zusammenfassung",
    "Summe aus Rufnummern und Verträgen\t100.00",
    "Rundungsdifferenz\t-0.05",
    "Rechnungsbetrag\t99.95",
])


def _positionen(text: str = _RECHNUNG) -> dict[str, Decimal]:
    """Die ``pos:``-Einträge des einen Befunds, ohne Präfix."""
    befund = _lies(text)[0]
    return {
        k[len(POS_PRAEFIX):]: Decimal(w)
        for k, w in befund.extras.items()
        if k.startswith(POS_PRAEFIX)
    }


# ------------------------------------------------------- Parser: Positionen


def test_jede_position_der_tabelle_wird_gelesen() -> None:
    assert _positionen() == {
        "blue Mobile M": Decimal("80.00"),
        "Connect Pack": Decimal("10.00"),
        "blue Benefit: Kombi-Rabatt": Decimal("-20.00"),
        "Telefonie, Spezialtarif": Decimal("4.00"),
        "blue home: blue Internet M": Decimal("30.00"),
        "Promotion Internet M": Decimal("-4.00"),
    }


def test_die_letzte_spalte_ist_der_betrag_nicht_der_preis() -> None:
    """Der Fall, für den es den Spaltentext überhaupt braucht.

    „Promotion Internet M  1  -4.00  -4.00" und
    „Promotion Internet M  1  -4.00" sehen in der Lesereihenfolge gleich aus.
    Hier steht in der Preisspalte etwas ANDERES als in der Betragsspalte —
    wer die erste Zahl nimmt, liest 9.00 statt 4.00.
    """
    text = _RECHNUNG.replace(
        "blue Mobile M\t1\t80.00\t80.00", "blue Mobile M\t1\t9.00\t80.00"
    )
    assert _positionen(text)["blue Mobile M"] == Decimal("80.00")


def test_summe_der_positionen_plus_rundung_ergibt_den_wert() -> None:
    """Die Gleichung, die am Modell als Konvention steht."""
    befund = _lies(_RECHNUNG)[0]
    summe = sum(_positionen().values(), Decimal("0"))
    assert summe + Decimal(befund.extras[POS_RUNDUNG]) == befund.value


def test_abschnittskopf_zaehlt_nicht_als_position() -> None:
    """„Abonnemente 80.00" ist die Zwischensumme des Abschnitts.

    Als Position gelesen stünde jeder Abschnitt doppelt in der Summe.
    """
    assert "Grundgebühren" not in _positionen()
    assert "Verbrauch" not in _positionen()


def test_kostenuebersicht_vor_dem_anker_wird_nicht_gelesen() -> None:
    """Seite 2 wiederholt dieselben Beträge als Gruppensummen.

    Ohne den Anker „Positionen im Detail" käme jede Rechnung auf das Doppelte
    — und ein Posten, den die Positionstabelle gar nicht führt, dazu.
    """
    assert "blue TV Irgendwas" not in _positionen()


def test_auslandsuebersicht_wird_nicht_doppelt_gezaehlt() -> None:
    """„Aufstellung nach Ziel im Ausland" schlüsselt Gezähltes erneut auf.

    Der Betrag steht bereits unter „Verbrauch"; ein zweites Mal gezählt
    macht aus vier Franken acht.
    """
    positionen = _positionen()
    assert "Telefonie" not in positionen  # der Eintrag des Auslandsblocks
    assert positionen["Telefonie, Spezialtarif"] == Decimal("4.00")


def test_auslandsuebersicht_sperrt_auch_ohne_zwischensumme_davor() -> None:
    """Die Sperre muss aus eigener Kraft greifen, nicht aus Versehen.

    In den vorliegenden Rechnungen steht zwischen dem Abschnitt „Verbrauch"
    und dem Auslandsblock immer noch eine Zeile „Summe" — schon die beendet den
    Abschnitt. Fiele sie einmal weg, liefe der Auslandsblock ohne eigene Sperre
    einfach im offenen Abschnitt weiter, und jeder Auslandsanruf stünde zweimal
    in der Summe. Genau diese Rechnung geht dann nicht mehr auf.
    """
    ohne_summe = _RECHNUNG.replace("Summe\t74.00\n", "")
    positionen = _positionen(ohne_summe)
    assert "Telefonie" not in positionen
    # Die Gegenprobe: die Rechnung geht weiterhin auf. Ohne Sperre stünden die
    # vier Franken zweimal darin und die Selbstprüfung schlüge fehl.
    assert sum(positionen.values(), Decimal("0")) == Decimal("100.00")


def test_betrag_braucht_zwei_nachkommastellen() -> None:
    """Die Menge ist keine Betragsspalte.

    Bleibt die Betragsspalte einer Zeile leer, ist die letzte Zelle die Menge —
    und „1" als Betrag gelesen ergäbe einen Franken, den niemand verlangt hat.
    Derselbe Fehlgriff ist in diesem Projekt schon einmal passiert: der
    Steuer-Parser las die Jahreszahl aus der Überschrift als Betrag.
    """
    ohne_betragsspalte = "\n".join([
        "Rechnung für März 2031",
        "Rechnungstotal inkl. MWST\t30.00",
        "Positionen im Detail",
        "Menge\tPreis pro Einheit\tBetrag",
        "Grundgebühren\t30.00",
        "blue Mobile M\t1\t30.00\t30.00",
        "Gratis-Option\t1",
        "Summe\t30.00",
    ])
    assert _positionen(ohne_betragsspalte) == {"blue Mobile M": Decimal("30.00")}


def test_zwischenueberschrift_beendet_den_abschnitt_nicht() -> None:
    """„Im Ausland abgehend" trägt keinen Betrag und steht über der Position.

    Beendete sie den Abschnitt, fiele die Zeile darunter aus der Summe — und
    die Selbstprüfung meldete einen Fehler, wo keiner ist.
    """
    assert "Telefonie, Spezialtarif" in _positionen()


def test_laufzeitzaehler_faellt_aus_dem_namen() -> None:
    """„(3/12)" zählt die Monate der Promotion, nicht das Produkt.

    Bliebe er stehen, entstünde für jeden Monat ein neuer Posten.
    """
    assert "Promotion Internet M" in _positionen()
    assert "Promotion Internet M (3/12)" not in _positionen()


def test_gleichnamige_positionen_werden_addiert() -> None:
    """Dasselbe Abonnement kann auf zwei Verträgen stehen."""
    text = _RECHNUNG.replace(
        "blue home: blue Internet M\t1\t30.00\t30.00",
        "blue Mobile M\t1\t30.00\t30.00",
    )
    assert _positionen(text)["blue Mobile M"] == Decimal("110.00")


# ------------------------------------------------- Parser: Kopf und Prüfung


def test_periode_ist_der_rechnungsmonat() -> None:
    befund = _lies(_RECHNUNG)[0]
    assert befund.slug == "beispielfunk"
    assert befund.period_start == date(2031, 3, 1)
    assert befund.period_end == date(2031, 3, 31)
    assert befund.value == Decimal("99.95")


def test_rundungsdifferenz_ist_keine_position() -> None:
    """Sie entsteht beim Runden der Summe, nicht durch einen Posten."""
    befund = _lies(_RECHNUNG)[0]
    assert befund.extras[POS_RUNDUNG] == "-0.05"
    assert "Rundungsdifferenz" not in _positionen()


def test_rabatt_ist_die_summe_der_negativen_positionen() -> None:
    """Positiv geschrieben: der Nebenwert heisst „Rabatte" und meint eine Höhe."""
    befund = _lies(_RECHNUNG)[0]
    assert befund.extras[POS_RABATT] == "24.00"


def test_rechnung_die_nicht_aufgeht_meldet_einen_fehler() -> None:
    """Der Kern des Auftrags: lieber ein fehlender Monat als eine falsche Zahl.

    Eine still verschluckte Position sähe im Verlauf aus wie eine gesenkte
    Rechnung — und niemand käme je auf die Idee, sie nachzuprüfen.
    """
    ohne_position = _RECHNUNG.replace("Connect Pack\t1\t10.00\t10.00\n", "")
    with pytest.raises(PruefsummeFehler):
        _lies(ohne_position)


def test_fehlender_rechnungsbetrag_liefert_nichts_statt_zu_raten() -> None:
    ohne_total = _RECHNUNG.replace("Rechnungstotal inkl. MWST\t99.95", "")
    assert _lies(ohne_total) == []


def test_fremder_belegtext_liefert_nichts() -> None:
    assert _lies("Vorsorgeausweis per 01.07.2026\nAHV-Jahreslohn 88'888") == []


# --------------------------------------------------- Spaltentext aus dem PDF


def _pdf_bauen(ziel: Path, zeilen: list[tuple[float, float, str]]) -> Path:
    """Ein PDF mit Wörtern an genau diesen Koordinaten (x, y, Text)."""
    import fitz

    doc = fitz.open()
    seite = doc.new_page()
    for x, y, wort in zeilen:
        seite.insert_text((x, y), wort, fontsize=8)
    doc.save(ziel)
    doc.close()
    return ziel


def test_spaltentext_trennt_menge_preis_und_betrag(tmp_path: Path) -> None:
    """Rechtsbündige Spalten bleiben getrennt, Wörter einer Zelle nicht.

    Die Koordinaten sind die der echten Rechnung: Positionsname ab 93,
    Menge um 333, Preis um 462, Betrag um 530.
    """
    pfad = _pdf_bauen(tmp_path / "p.pdf", [
        (93, 100, "blue"), (110, 100, "Mobile"), (136, 100, "M"),
        (333, 100, "1"), (462, 100, "80.00"), (530, 100, "80.00"),
    ])
    assert skript.pdf_spalten(pfad).strip() == "blue Mobile M\t1\t80.00\t80.00"


def test_spaltentext_wirft_den_zeitraum_aus_der_zeile(tmp_path: Path) -> None:
    """Der Zeitraum sitzt acht Punkte neben der Menge.

    Bliebe er stehen, klebte er mit ihr in einer Zelle — und die letzte Zelle
    wäre nicht mehr verlässlich der Betrag.

    Der Bindestrich steht hier bündig am ersten Datum: so setzt PyMuPDF beides
    zu einem einzigen Wort zusammen, und genau dieses Bruchstück blieb ohne
    Duldung des angehängten Strichs als eigene Zelle stehen.
    """
    pfad = _pdf_bauen(tmp_path / "p.pdf", [
        (93, 100, "Promotion"), (132, 100, "Netflix"),
        (333, 100, "1"),
        (345, 100, "01.03.31"), (377, 100, "-"), (381, 100, "30.03.31"),
        (460, 100, "-4.00"), (528, 100, "-4.00"),
    ])
    assert skript.pdf_spalten(pfad).strip() == "Promotion Netflix\t1\t-4.00\t-4.00"


def test_spaltentext_fasst_eine_tabellenzeile_zusammen(tmp_path: Path) -> None:
    """Zwei Grundlinien, die um weniger als drei Punkte auseinanderliegen,
    gehören zur selben Zeile — mehr nicht."""
    pfad = _pdf_bauen(tmp_path / "p.pdf", [
        (93, 100, "oben"), (300, 101, "auch-oben"), (93, 130, "unten"),
    ])
    assert skript.pdf_spalten(pfad).splitlines() == ["oben\tauch-oben", "unten"]


def test_rechnungsbelege_werden_ueber_die_ganze_ordnerkette_gefunden() -> None:
    """Die Rechnungen liegen im Unterordner ``Beispielfunk/Rechnungen``.

    Würde nur der unmittelbare Elternordner befragt, hiesse er „Rechnungen"
    und kein einziger Beleg wäre noch zugeordnet.
    """
    assert skript.parser_fuer(
        Path("Beispielfunk/Rechnungen/20310403_Beispielfunk_Rechnung_Maerz_2031.pdf")
    ) == "beispielfunk"


def test_verbindungsnachweis_liefert_keinen_wert() -> None:
    """Er liegt im selben Ordner, ist aber keine Rechnung.

    Ihn am Dateinamen auszuschliessen wäre die schwächere Sicherung — ein Name
    lässt sich umbenennen. Der Parser sieht den Inhalt: ohne Rechnungskopf
    entsteht kein Befund, und zwar ohne Fehlermeldung, denn hier ist nichts kaputt.
    """
    assert skript.parser_fuer(
        Path("Beispielfunk/Verbindungsnachweise/20310331_Beispielfunk_Verbindungsnachweis.pdf")
    ) == "beispielfunk"
    nachweis = "\n".join([
        "Verbindungsnachweis",
        "Aufstellung nach Ziel im Ausland\tDienst\tLand\tBetrag",
        "Telefonie\tIrgendwoland\t4.00",
        "Total\t4.00",
    ])
    assert _lies(nachweis) == []


# ------------------------------------------------------------ Zusammenfassen


def test_monatswerte_werden_nicht_addiert() -> None:
    """Zwei Belege für denselben Monat: einer korrigiert den anderen.

    Additiv geführt stünde der doppelte Betrag im Verlauf und jede Position
    zählte zweimal — die Gleichung am Punkt wäre still verletzt.
    """
    roh = [
        {"slug": "beispielfunk", "start": "2031-03-01", "ende": "2031-03-31",
         "wert": "99.95", "extras": {"pos:A": "99.95"}, "additiv": False, "quelle": "alt.pdf"},
        {"slug": "beispielfunk", "start": "2031-03-01", "ende": "2031-03-31",
         "wert": "88.85", "extras": {"pos:A": "88.85"}, "additiv": False, "quelle": "neu.pdf"},
    ]
    zusammen = skript.zusammenfassen(roh)
    assert len(zusammen) == 1
    assert zusammen[0]["wert"] == "88.85"
    assert zusammen[0]["extras"] == {"pos:A": "88.85"}


def test_additive_reihen_summieren_nur_zahlen() -> None:
    """Ein Nebenwert muss kein Betrag sein — ``rechnungsart`` ist Text.

    Ohne diese Weiche riss die Summenbildung den ganzen Lauf ab, sobald ein
    Parser mit Text-Nebenwerten additiv würde.
    """
    roh = [
        {"slug": "gesundheit_selbst", "start": "2031-01-01", "ende": "2031-12-31",
         "wert": "10.00", "extras": {"franchise": "10.00", "art": "erste"},
         "additiv": True, "quelle": "a.pdf"},
        {"slug": "gesundheit_selbst", "start": "2031-01-01", "ende": "2031-12-31",
         "wert": "20.00", "extras": {"franchise": "20.00", "art": "zweite"},
         "additiv": True, "quelle": "b.pdf"},
    ]
    zusammen = skript.zusammenfassen(roh)
    assert zusammen[0]["extras"] == {"franchise": "30.00", "art": "zweite"}


# ------------------------------------------------------------------- Reihe


def test_mitgeliefertes_beispielprofil_legt_keine_reihe_an() -> None:
    """Eine frische Installation bekommt keine Reihe für einen erfundenen Anbieter.

    Das ist die eine Hälfte der Entscheidung: Verlaufsreihen entstehen NUR aus
    eigenen Profilen. Ein mitgeliefertes Beispiel zeigt, wie eine Profildatei
    aussieht — es soll aber niemandem eine Reihe für einen Dienst hinstellen,
    den er gar nicht hat.
    """
    with SessionLocal() as db:
        assert db.scalar(select(MetricSeries).where(MetricSeries.slug == "beispielfunk")) is None


def test_ein_eigenes_profil_ergibt_eine_vollstaendige_reihe(tmp_path) -> None:
    """Die andere Hälfte: aus einem eigenen Profil entsteht eine brauchbare Reihe.

    Geprüft wird an der Ableitung und nicht am Seed-Lauf. Der Seed hängt an der
    Datenbank der Testsitzung; die Ableitung ist die Stelle, an der die
    Entscheidung wirklich fällt.
    """
    from moneten.db.seeds import _Reihe
    from moneten.services.anbieter_profil import ist_eigenes, lies_profil

    vorlage = Path(__file__).resolve().parents[1] / "src/moneten/anbieter/beispielfunk.toml"
    eigen = tmp_path / "eigenfunk.toml"
    eigen.write_text(
        vorlage.read_text(encoding="utf-8").replace('slug = "beispielfunk"', 'slug = "eigenfunk"'),
        encoding="utf-8",
    )

    profil = lies_profil(eigen)
    assert ist_eigenes(profil), "Ein Profil ausserhalb der Mitlieferung gilt als eigenes"

    reihe = _Reihe(
        slug=profil.slug, name=profil.name, unit=MetricUnit.CHF,
        cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE,
        note=profil.notiz, kategorie=profil.kategorie,
        nebenwert="rabatt", nebeneinheit=MetricUnit.CHF, nebenlabel="Rabatte",
    )
    assert reihe.slug == "eigenfunk"
    # Ohne Kategorie liefe der Soll/Ist-Abgleich ins Leere.
    assert reihe.kategorie == "Handy-Abo"
    assert reihe.nebenwert == "rabatt"


def test_die_reihe_des_eigenen_anbieters_ist_verknuepft() -> None:
    """Und in DIESER Installation steht die Reihe wirklich in der Datenbank."""
    from moneten.db.seeds import _reihen_aus_anbieterprofilen

    eigene = _reihen_aus_anbieterprofilen()
    if not eigene:
        pytest.skip("Diese Installation hat kein eigenes Anbieterprofil")
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == eigene[0].slug))
        assert reihe is not None
        assert reihe.cadence == MetricCadence.MONATLICH
        assert reihe.kind == MetricKind.AUSGABE
        assert reihe.unit == MetricUnit.CHF
        assert reihe.secondary_key == "rabatt"
        assert reihe.category_id is not None
        kategorie = db.get(Category, reihe.category_id)
        assert kategorie is not None and kategorie.name == "Handy-Abo"


# ------------------------------------------------------------------- Import


@pytest.fixture()
def reihe_zum_beispielprofil():
    """Legt die Reihe an, in die importiert wird — und räumt sie wieder weg.

    Der Seed legt für ein MITGELIEFERTES Profil bewusst keine Reihe an (siehe
    ``test_mitgeliefertes_beispielprofil_legt_keine_reihe_an``). Die Tests hier
    prüfen aber den Import, nicht den Seed; sie brauchen ihre Reihe also selbst.
    Das ist auch ehrlicher: was ein Test voraussetzt, soll er herstellen.
    """
    with SessionLocal() as db:
        vorhanden = db.scalar(select(MetricSeries).where(MetricSeries.slug == "beispielfunk"))
        if vorhanden is not None:
            yield vorhanden.id
            return
        reihe = MetricSeries(
            slug="beispielfunk", name="Beispielfunk AG", unit=MetricUnit.CHF,
            cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE,
            secondary_key="rabatt", secondary_unit=MetricUnit.CHF,
            secondary_label="Rabatte", note="Nur für diesen Test.",
        )
        db.add(reihe)
        db.commit()
        kennung = reihe.id
    yield kennung
    with SessionLocal() as db:
        db.query(MetricPoint).filter(MetricPoint.series_id == kennung).delete()
        db.query(MetricSeries).filter(MetricSeries.id == kennung).delete()
        db.commit()


def _datei(befunde: list[dict]) -> dict:
    inhalt = json.dumps({"version": 1, "befunde": befunde}, ensure_ascii=False)
    return {"files": {"datei": ("verlaeufe.json", inhalt.encode("utf-8"), "application/json")}}


def _befund(**abweichend: object) -> dict:
    grund = {
        "slug": "beispielfunk", "start": "2031-03-01", "ende": "2031-03-31",
        "wert": "99.95", "quelle": "erfunden.pdf",
        "extras": {"pos:blue Mobile M": "80.00", "pos:Rabatt": "-0.05", "rabatt": "0.05"},
    }
    return {**grund, **abweichend}


def test_import_uebernimmt_die_positionen(logged_in_client: TestClient, reihe_zum_beispielprofil) -> None:
    antwort = logged_in_client.post("/verlaeufe/import", **_datei([_befund()]))
    assert antwort.status_code == 200, antwort.text
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "beispielfunk"))
        punkt = db.scalar(
            select(MetricPoint).where(
                MetricPoint.series_id == reihe.id,
                MetricPoint.period_start == date(2031, 3, 1),
            )
        )
        assert punkt is not None
        assert punkt.extras["pos:blue Mobile M"] == "80.00"
        assert punkt.extras["rabatt"] == "0.05"


def test_import_lehnt_eine_position_ohne_betrag_ab(logged_in_client: TestClient, reihe_zum_beispielprofil) -> None:
    """Ein Text als Positionsbetrag käme sonst bis in den gestapelten Balken.

    Dort führte er zu einer Fehlerseite statt zu einer Meldung an der Datei,
    aus der er stammt.
    """
    befund = _befund(start="2031-04-01", ende="2031-04-30",
                     extras={"pos:blue Mobile M": "ungefähr 80"})
    antwort = logged_in_client.post("/verlaeufe/import", **_datei([befund]))
    assert antwort.status_code == 200
    assert "pos:blue Mobile M" in antwort.text
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "beispielfunk"))
        assert db.scalar(
            select(MetricPoint).where(
                MetricPoint.series_id == reihe.id,
                MetricPoint.period_start == date(2031, 4, 1),
            )
        ) is None


def test_import_begrenzt_die_zahl_der_nebenwerte(logged_in_client: TestClient, reihe_zum_beispielprofil) -> None:
    """Hundert Positionen sind keine gelesene Rechnung mehr, sondern ein Fehler."""
    viele = {f"pos:P{i}": "1.00" for i in range(100)}
    befund = _befund(start="2031-05-01", ende="2031-05-31", extras=viele)
    antwort = logged_in_client.post("/verlaeufe/import", **_datei([befund]))
    assert antwort.status_code == 200
    assert "zu viele" in antwort.text
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "beispielfunk"))
        assert db.scalar(
            select(MetricPoint).where(
                MetricPoint.series_id == reihe.id,
                MetricPoint.period_start == date(2031, 5, 1),
            )
        ) is None


# ---------------------------------------------------------------------------
# Eigene Ordner-Zuordnung (aus den Daten, nicht aus dem Code)
# ---------------------------------------------------------------------------


@pytest.fixture
def zuordnung_zuruecksetzen():
    """Die Zuordnung ist Modul-Zustand — nach dem Test steht sie wie vorher.

    Ohne das faerbte ein Test auf den naechsten ab: die eigenen Eintraege
    stehen vorne und gewinnen, der uebernaechste Test suchte dann seinen
    Ordner und fand den fremden.
    """
    alt_zuordnung = list(skript.ZUORDNUNG)
    alt_tabu = set(skript.TABU)
    yield
    skript.ZUORDNUNG[:] = alt_zuordnung
    skript.TABU.clear()
    skript.TABU.update(alt_tabu)


def test_eigene_zuordnung_gewinnt_vor_der_vorgabe(tmp_path, zuordnung_zuruecksetzen) -> None:
    """Ein eigener Ordnername muss die allgemeine Vorgabe schlagen.

    Der Grund, warum es diese Datei ueberhaupt gibt: die eigenen Ordner heissen
    nach einem Vermieter, einem Kanton, einer Bank. Stuenden sie im Skript,
    stuenden sie in jedem Export. Stehen sie hinten statt vorn, greift
    stattdessen die allgemeine Regel und der Beleg landet beim falschen Parser.
    """
    datei = tmp_path / "zuordnung.toml"
    datei.write_text(
        'tabu = ["hausbank"]\n'
        '\n'
        '[[zuordnung]]\n'
        'parser = "miete"\n'
        'ordner = "2019_wohnung_am_park"\n'
        'dateien = ["vertrag"]\n',
        encoding="utf-8",
    )
    skript.lies_zuordnung(datei)

    assert skript.parser_fuer(Path("2019_Wohnung_am_Park/Vertrag_2019.pdf")) == "miete"
    assert "hausbank" in skript.TABU
    # Die Vorgaben bleiben daneben gueltig.
    assert skript.parser_fuer(Path("Policen/Versicherungspolice_2025.pdf")) == "police"


def test_fehlende_zuordnungsdatei_ist_kein_fehler(tmp_path, zuordnung_zuruecksetzen) -> None:
    """Ohne eigene Datei laeuft das Skript mit den Vorgaben weiter.

    Wer die App frisch aufsetzt, hat keine — ein Abbruch an dieser Stelle
    hiesse: erst eine Datei schreiben, dann erfahren, wozu.
    """
    vorher = len(skript.ZUORDNUNG)
    skript.lies_zuordnung(tmp_path / "gibt-es-nicht.toml")
    assert len(skript.ZUORDNUNG) == vorher


def test_zuordnung_ohne_parser_faellt_auf(tmp_path, zuordnung_zuruecksetzen) -> None:
    """Ein Eintrag ohne Ziel wird gemeldet, nicht uebergangen.

    Still ignoriert waere er das Schlimmste: die Extraktion liefe durch, der
    Ordner bliebe unzugeordnet, und in der Verlaufsreihe fehlten Jahre — ohne
    dass irgendwo etwas rot wird.
    """
    datei = tmp_path / "zuordnung.toml"
    datei.write_text('[[zuordnung]]\nordner = "irgendwas"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="parser"):
        skript.lies_zuordnung(datei)
