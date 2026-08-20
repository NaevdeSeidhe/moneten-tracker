"""Verlaufsreihen: schreiben, lesen, vergleichen.

Alle Zahlen hier sind erfunden und bewusst rund — 100 auf 130, nicht 312.45 auf
338.60. Die Tests messen die Rechnung, nicht den Bestand.

Jeder Test legt sich eine **eigene** Reihe an und räumt sie wieder weg. Die
Testläufe teilen sich eine Datenbank; ein liegengebliebener Punkt in einer
Seed-Reihe wie „Strom" würde die Auswertung des nächsten Tests verfälschen und
den Fehlschlag an einer Stelle melden, die damit nichts zu tun hat.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from moneten.db.models import MetricCadence, MetricKind, MetricSeries, MetricUnit
from moneten.db.session import SessionLocal
from moneten.services.metrics import (
    alle_verlaeufe,
    formatiere,
    loesche_punkt,
    periode_aus_takt,
    reihe_nach_slug,
    reihen,
    setze_punkt,
    verlauf,
)

JAN = date(2014, 1, 1)
FEB = date(2014, 2, 1)
MRZ = date(2014, 3, 1)


@contextmanager
def _reihe(
    db: Session,
    *,
    takt: MetricCadence = MetricCadence.MONATLICH,
    einheit: MetricUnit = MetricUnit.CHF,
    art: MetricKind = MetricKind.AUSGABE,
    nebenwert: str | None = None,
    archiviert: bool = False,
) -> Iterator[MetricSeries]:
    """Eine eigene Reihe für die Dauer eines Tests.

    Das Aufräumen steht im ``finally``: ein fehlgeschlagener Test soll seine
    Reihe nicht in der gemeinsamen Datenbank zurücklassen und Folgefehler
    erzeugen, die mit der Ursache nichts zu tun haben.
    """
    marke = uuid.uuid4().hex[:6]
    r = MetricSeries(
        slug=f"zzz_{marke}",
        name=f"ZZZ Testreihe {marke}",
        unit=einheit,
        cadence=takt,
        kind=art,
        secondary_key=nebenwert,
        archived=archiviert,
    )
    db.add(r)
    db.commit()
    try:
        yield r
    finally:
        db.delete(r)  # cascade="all, delete-orphan" räumt die Punkte mit weg
        db.commit()


def _punkte(db: Session, reihe: MetricSeries, werte: dict[date, str]) -> None:
    """Monatswerte anlegen — der Takt liefert das Periodenende."""
    for start, wert in werte.items():
        setze_punkt(
            db, reihe,
            start=start,
            ende=periode_aus_takt(MetricCadence.MONATLICH, start),
            wert=Decimal(wert),
        )
    db.commit()


# ---------------------------------------------------------------- Schreiben


def test_setze_punkt_legt_an_und_aktualisiert_dieselbe_periode() -> None:
    """Die Periode ist der Schlüssel, nicht die Quelle.

    Trägt man einen Monat von Hand nach, den später doch noch ein Beleg
    liefert, soll daraus ein Wert werden und nicht zwei.
    """
    with SessionLocal() as db, _reihe(db) as r:
        p1, neu1 = setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("310.00"))
        db.commit()
        assert neu1 is True

        p2, neu2 = setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("360.00"))
        db.commit()
        assert neu2 is False, "Derselbe Monat zum zweiten Mal ist kein neuer Punkt"
        assert p2.id == p1.id

        punkte = verlauf(db, r).punkte
        assert len(punkte) == 1, "Dieselbe Periode zweimal erfasst → ein Punkt, nicht zwei"
        assert punkte[0].wert == Decimal("360.00")


def test_ueberschreiben_false_laesst_den_vorhandenen_wert_stehen() -> None:
    """So darf der Einmal-Import beliebig oft laufen.

    Ohne diese Zusicherung machte er jede von Hand nachgetragene Korrektur beim
    nächsten Lauf wieder platt.
    """
    with SessionLocal() as db, _reihe(db) as r:
        setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31),
                    wert=Decimal("310.00"), quelle="von Hand")
        db.commit()

        punkt, neu = setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31),
                                 wert=Decimal("999.00"), quelle="import.pdf",
                                 ueberschreiben=False)
        db.commit()

        assert neu is False
        assert punkt.value == Decimal("310.00")
        assert punkt.source == "von Hand"
        assert verlauf(db, r).aktuell.wert == Decimal("310.00")


def test_ueberschreiben_ersetzt_auch_periodenende_herkunft_und_nebenwerte() -> None:
    """Ein neuer Beleg hinterlässt keine Reste des alten.

    Bliebe etwa ein alter ``kwh``-Wert stehen, während der Betrag schon der neue
    ist, stünde in der Kurve ein Rappenpreis, den es nie gab.
    """
    with SessionLocal() as db, _reihe(db, nebenwert="kwh") as r:
        setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("300.00"),
                    extras={"kwh": "100"}, quelle="alt.pdf", notiz="erste Lesung")
        db.commit()

        setze_punkt(db, r, start=JAN, ende=date(2014, 3, 31), wert=Decimal("400.00"),
                    extras={"kwh": "200"}, quelle="neu.pdf", notiz="korrigiert")
        db.commit()

        aktuell = verlauf(db, r).aktuell
        assert aktuell.ende == date(2014, 3, 31)
        assert aktuell.neben == Decimal("200")
        assert aktuell.quelle == "neu.pdf"
        assert aktuell.notiz == "korrigiert"


def test_loesche_punkt_entfernt_den_wert() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        punkt, _ = setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("310.00"))
        db.commit()

        assert loesche_punkt(db, punkt.id) is True
        db.commit()
        assert verlauf(db, r).leer is True


def test_loeschen_eines_unbekannten_punkts_meldet_false_statt_zu_werfen() -> None:
    """Zweimal auf „Löschen" getippt ist am Handy der Normalfall, kein Fehler."""
    with SessionLocal() as db, _reihe(db) as r:
        punkt, _ = setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("310.00"))
        db.commit()
        weg = punkt.id
        loesche_punkt(db, weg)
        db.commit()

        assert loesche_punkt(db, weg) is False


# ---------------------------------------------------------------- Verlauf


def test_erster_punkt_hat_keine_veraenderung() -> None:
    """Es gibt keinen Vorwert — „0 %" wäre eine Behauptung, keine Messung."""
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "100.00", FEB: "150.00"})
        punkte = verlauf(db, r).punkte
        assert punkte[0].diff_pct is None
        assert punkte[1].diff_pct == Decimal("50.0")


def test_diff_pct_misst_gegen_den_vorwert_nicht_gegen_den_ersten() -> None:
    """100 → 200 → 300 sind +100 % und dann +50 %, nicht +100 % und +200 %."""
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "100.00", FEB: "200.00", MRZ: "300.00"})
        assert [p.diff_pct for p in verlauf(db, r).punkte] == [
            None, Decimal("100.0"), Decimal("50.0"),
        ]


def test_punkte_kommen_chronologisch_egal_wann_erfasst() -> None:
    """Nachträglich erfasste Monate dürfen die Veränderungsrechnung nicht verdrehen."""
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {MRZ: "300.00"})
        _punkte(db, r, {JAN: "100.00"})
        _punkte(db, r, {FEB: "200.00"})

        v = verlauf(db, r)
        assert [p.start for p in v.punkte] == [JAN, FEB, MRZ]
        assert [p.diff_pct for p in v.punkte] == [None, Decimal("100.0"), Decimal("50.0")]


def test_vorwert_null_gibt_keine_veraenderung_statt_zu_werfen() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "0.00", FEB: "50.00"})
        assert verlauf(db, r).punkte[1].diff_pct is None


def test_gesamt_pct_misst_vom_ersten_zum_letzten_wert() -> None:
    """Der Ausreisser dazwischen (500) gehört nicht in die Gesamtbilanz."""
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "200.00", FEB: "500.00", MRZ: "260.00"})
        assert verlauf(db, r).gesamt_pct == Decimal("30.0")


def test_gesamt_pct_eines_einzelnen_punkts_ist_none() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "200.00"})
        assert verlauf(db, r).gesamt_pct is None


def test_gesamt_pct_ab_null_ist_none_statt_unendlich() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "0.00", FEB: "250.00"})
        assert verlauf(db, r).gesamt_pct is None


def test_leere_reihe_hat_weder_aktuell_noch_erster() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        v = verlauf(db, r)
        assert v.leer is True
        assert v.aktuell is None
        assert v.erster is None
        assert v.gesamt_pct is None


def test_erster_und_aktuell_zeigen_die_raender_der_reihe() -> None:
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "100.00", FEB: "200.00", MRZ: "300.00"})
        v = verlauf(db, r)
        assert v.erster.wert == Decimal("100.00")
        assert v.aktuell.wert == Decimal("300.00")


def test_nebenwert_kommt_aus_den_extras() -> None:
    """Erst mit den kWh daneben ist eine höhere Rechnung als „mehr verbraucht"
    oder „teurer geworden" lesbar."""
    with SessionLocal() as db, _reihe(db, nebenwert="kwh") as r:
        setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("640.00"),
                    extras={"kwh": "800", "rp_kwh": "80.00"})
        db.commit()
        assert verlauf(db, r).aktuell.neben == Decimal("800")


def test_reihe_ohne_nebenwert_laesst_das_feld_leer() -> None:
    """Die Extras stehen trotzdem zur Verfügung — nur eben nicht als zweite Achse."""
    with SessionLocal() as db, _reihe(db) as r:
        setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("640.00"),
                    extras={"kwh": "800"})
        db.commit()
        aktuell = verlauf(db, r).aktuell
        assert aktuell.neben is None
        assert aktuell.extras["kwh"] == "800"


def test_unlesbarer_nebenwert_macht_den_punkt_nicht_kaputt() -> None:
    """Ein Lesefehler im Nebenwert darf den Hauptwert nicht mitreissen."""
    with SessionLocal() as db, _reihe(db, nebenwert="kwh") as r:
        setze_punkt(db, r, start=JAN, ende=date(2014, 1, 31), wert=Decimal("640.00"),
                    extras={"kwh": "nicht abgelesen"})
        db.commit()
        aktuell = verlauf(db, r).aktuell
        assert aktuell.neben is None
        assert aktuell.wert == Decimal("640.00")


# ---------------------------------------------------------------- Perioden


def test_periode_aus_takt_monatlich_endet_am_monatsletzten() -> None:
    """Februar 2024 hat 29 Tage — ein festes „30" oder „28" fiele hier auf."""
    assert periode_aus_takt(MetricCadence.MONATLICH, date(2024, 2, 10)) == date(2024, 2, 29)
    assert periode_aus_takt(MetricCadence.MONATLICH, date(2025, 2, 10)) == date(2025, 2, 28)
    assert periode_aus_takt(MetricCadence.MONATLICH, date(2023, 4, 1)) == date(2023, 4, 30)


def test_periode_aus_takt_monatlich_im_dezember() -> None:
    """Der Dezember ist der einzige Monat ohne Folgemonat im selben Jahr."""
    assert periode_aus_takt(MetricCadence.MONATLICH, date(2023, 12, 5)) == date(2023, 12, 31)


def test_periode_aus_takt_quartalsweise_umfasst_drei_monate() -> None:
    assert periode_aus_takt(MetricCadence.QUARTALSWEISE, date(2024, 1, 1)) == date(2024, 3, 31)
    assert periode_aus_takt(MetricCadence.QUARTALSWEISE, date(2024, 10, 1)) == date(2024, 12, 31)


def test_periode_aus_takt_quartalsweise_ueber_den_jahreswechsel() -> None:
    """Ein Quartal ab November endet im Januar des Folgejahrs, nicht im Monat 13."""
    assert periode_aus_takt(MetricCadence.QUARTALSWEISE, date(2023, 11, 1)) == date(2024, 1, 31)
    assert periode_aus_takt(MetricCadence.QUARTALSWEISE, date(2023, 12, 1)) == date(2024, 2, 29)


def test_periode_aus_takt_jaehrlich_endet_am_jahresende() -> None:
    assert periode_aus_takt(MetricCadence.JAEHRLICH, date(2022, 5, 17)) == date(2022, 12, 31)


def test_periode_aus_takt_unregelmaessig_ist_ein_stichtag() -> None:
    """Ein Altersguthaben gilt an einem Tag, nicht über eine Spanne."""
    assert periode_aus_takt(MetricCadence.UNREGELMAESSIG, date(2022, 5, 17)) == date(2022, 5, 17)


# ---------------------------------------------------------------- Auffälligste


def test_archivierte_reihe_bleibt_der_uebersicht_fern() -> None:
    """Archivieren heisst: nicht mehr zeigen — aber auch nicht löschen."""
    with SessionLocal() as db, _reihe(db, archiviert=True) as r:
        assert r.slug not in {x.slug for x in reihen(db)}
        assert r.slug in {x.slug for x in reihen(db, mit_archivierten=True)}
        assert r.id not in {v.reihe.id for v in alle_verlaeufe(db)}


def test_reihe_nach_slug_findet_auch_archivierte() -> None:
    """Der Slug ist der stabile Schlüssel — er muss unabhängig vom Zustand greifen."""
    with SessionLocal() as db, _reihe(db, archiviert=True) as r:
        gefunden = reihe_nach_slug(db, r.slug)
        assert gefunden is not None and gefunden.id == r.id


def test_reihe_nach_unbekanntem_slug_ist_none() -> None:
    with SessionLocal() as db:
        assert reihe_nach_slug(db, "zzz_gibt_es_nicht") is None


def test_alle_verlaeufe_liefert_die_punkte_gleich_mit() -> None:
    """Ein Rundgang für die Übersichtsseite — mit leeren Punktlisten wäre er nutzlos."""
    with SessionLocal() as db, _reihe(db) as r:
        _punkte(db, r, {JAN: "100.00", FEB: "150.00"})
        v = next(x for x in alle_verlaeufe(db) if x.reihe.id == r.id)
        assert [p.wert for p in v.punkte] == [Decimal("100.00"), Decimal("150.00")]
        assert v.aktuell.diff_pct == Decimal("50.0")


# ---------------------------------------------------------------- Formatierung


def test_formatiere_setzt_das_schweizer_hochkomma() -> None:
    assert formatiere(Decimal("12345.60"), MetricUnit.CHF) == "12'345.60"


def test_formatiere_zeigt_franken_immer_mit_rappen() -> None:
    assert formatiere(Decimal("7.5"), MetricUnit.CHF) == "7.50"


def test_formatiere_kwh_ohne_nachkommastellen() -> None:
    """Ein Zähler zeigt ganze Kilowattstunden; Rappen wären hier Scheingenauigkeit."""
    assert formatiere(Decimal("1234"), MetricUnit.KWH) == "1'234 kWh"


def test_formatiere_prozent_mit_einer_nachkommastelle() -> None:
    assert formatiere(Decimal("80"), MetricUnit.PROZENT) == "80.0 %"
