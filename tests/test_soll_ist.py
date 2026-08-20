"""Merkt die App, wenn eine Belastung fehlt, abweicht oder ein Fixposten veraltet?

Die Verlaufsreihen wissen aus den Belegen, was eine Leistung kosten *soll*. Die
Buchungen wissen, was wirklich abging. Zwischen beidem klafft im Alltag genau
dann eine Lücke, wenn es teuer wird: eine Prämie steigt zum Jahreswechsel, der
Fixposten in der App bleibt auf dem Vorjahreswert stehen, und das Budget rechnet
ab dann dauerhaft zu tief — ohne dass irgendetwas Alarm schlägt.

Diese Tests sind der Grund, warum sich darauf verlassen lässt. Ohne sie wäre der
Abgleich nur eine Behauptung.

ALLE BETRÄGE HIER SIND ERFUNDEN. Die 777er-Reihe ist absichtlich unrealistisch,
damit sie nie mit echten Daten verwechselt wird.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from moneten.db.models import (
    Account,
    BudgetInterval,
    ManualSubscription,
    MetricKind,
    MetricPoint,
    MetricSeries,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services import metrics, soll_ist
from moneten.services.metrics import reihe_nach_slug, setze_punkt

# Weit in der Zukunft, damit nichts mit Seed- oder anderen Testdaten kollidiert.
JAHR = 2031
PRAEMIE = Decimal("777.00")


@contextmanager
def _db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


@pytest.fixture
def reihe() -> Iterator[tuple[int, int]]:
    """Eine frische Reihe samt Kategorie; räumt hinterher restlos auf.

    Eigene Reihe statt der geseedeten ``kk_praemie``: Tests laufen in derselben
    DB, und eine gemeinsam benutzte Reihe liesse sie voneinander abhängen.
    """
    slug = f"test_{uuid.uuid4().hex[:8]}"
    with _db() as db:
        vorbild = metrics.reihe_nach_slug(db, "kk_praemie")
        assert vorbild is not None, "Seed fehlt — kk_praemie muss existieren"
        r = MetricSeries(
            slug=slug, name="Erfundene Reihe", unit=vorbild.unit,
            cadence=vorbild.cadence, kind=MetricKind.AUSGABE,
            category_id=vorbild.category_id,
        )
        db.add(r)
        db.commit()
        ids = (r.id, r.category_id)
    yield ids
    with _db() as db:
        db.query(Transaction).filter(
            Transaction.date >= date(JAHR, 1, 1),
            Transaction.date < date(JAHR + 1, 1, 1),
        ).delete()
        db.query(ManualSubscription).filter(
            ManualSubscription.name == "Erfundener Fixposten"
        ).delete()
        obj = db.get(MetricSeries, ids[0])
        if obj is not None:
            db.delete(obj)
        db.commit()


def _soll(db: Session, reihen_id: int, monat: int, wert: Decimal = PRAEMIE) -> None:
    r = db.get(MetricSeries, reihen_id)
    start = date(JAHR, monat, 1)
    metrics.setze_punkt(
        db, r, start=start, ende=metrics.periode_aus_takt(r.cadence, start), wert=wert
    )
    db.commit()


def _buchung(db: Session, kategorie_id: int, tag: date, betrag: str) -> None:
    konto = db.query(Account).first()
    db.add(Transaction(
        account_id=konto.id, date=tag, amount=Decimal(betrag),
        description="Erfundene Testbuchung", category_id=kategorie_id,
    ))
    db.commit()


def test_passende_buchung_gilt_als_sauber(reihe: tuple[int, int]) -> None:
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 1)
        _buchung(db, kategorie_id, date(JAHR, 1, 15), f"-{PRAEMIE}")
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert a.geprueft
    assert len(a.perioden) == 1
    p = a.perioden[0]
    # Der Belegwert steht positiv, die Ausgabe negativ — das Vorzeichen muss
    # gedreht werden, sonst meldet jede korrekte Zahlung die doppelte Differenz.
    assert p.ist == PRAEMIE
    assert p.differenz == Decimal("0")
    assert p.passt
    assert a.anzahl_fehlend == 0
    assert a.anzahl_abweichend == 0


def test_fehlende_buchung_wird_gemeldet(reihe: tuple[int, int]) -> None:
    reihen_id, _ = reihe
    with _db() as db:
        _soll(db, reihen_id, 3)
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    p = a.perioden[0]
    assert p.buchungen == 0
    assert p.fehlt
    assert not p.passt
    assert a.anzahl_fehlend == 1
    assert not a.sauber


def test_abweichender_betrag_wird_gemeldet(reihe: tuple[int, int]) -> None:
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 4)
        _buchung(db, kategorie_id, date(JAHR, 4, 10), "-700.00")
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    p = a.perioden[0]
    assert not p.fehlt
    assert not p.passt
    assert p.differenz == Decimal("-77.00")
    assert a.anzahl_abweichend == 1


def test_rappendifferenz_gilt_noch_als_passend(reihe: tuple[int, int]) -> None:
    """Rundung und Teilmonate erzeugen Kleinstdifferenzen — die sind kein Befund.

    Ohne Toleranz meldete der Abgleich bei fast jeder Reihe etwas, und eine
    Warnung, die immer leuchtet, liest niemand mehr.
    """
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 5)
        _buchung(db, kategorie_id, date(JAHR, 5, 10), "-777.40")
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    # 777.40 belastet gegen 777.00 verlangt: 40 Rappen zu viel bezahlt.
    assert a.perioden[0].differenz == Decimal("0.40")
    assert a.perioden[0].passt


def test_buchung_im_folgemonat_zaehlt_zur_periode(reihe: tuple[int, int]) -> None:
    """Die Februarprämie wird oft Ende Januar oder Anfang Februar belastet.

    Auf den Tag genau geschnitten, meldete der Abgleich einen Grossteil real
    vorhandener Buchungen als „fehlt" — die Periode reicht darum bis zum Ende
    des Monats, in dem sie endet.
    """
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 6)
        _buchung(db, kategorie_id, date(JAHR, 6, 28), f"-{PRAEMIE}")
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert a.perioden[0].buchungen == 1
    assert a.perioden[0].passt


def test_fixposten_fehlt(reihe: tuple[int, int]) -> None:
    reihen_id, _ = reihe
    with _db() as db:
        _soll(db, reihen_id, 7)
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert a.abo is not None
    assert not a.abo.erfasst


def test_veralteter_fixposten_wird_erkannt(reihe: tuple[int, int]) -> None:
    """Der teuerste Fall: die Prämie ist gestiegen, das Budget rechnet weiter alt."""
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 8)
        db.add(ManualSubscription(
            name="Erfundener Fixposten", amount=Decimal("700.00"),
            interval=BudgetInterval.MONATLICH, kind="fix", category_id=kategorie_id,
        ))
        db.commit()
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert a.abo.erfasst
    assert a.abo.hinterlegt == Decimal("700.00")
    assert a.abo.erwartet == PRAEMIE
    assert a.abo.veraltet
    assert not a.sauber


def test_aktueller_fixposten_ist_nicht_veraltet(reihe: tuple[int, int]) -> None:
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 9)
        db.add(ManualSubscription(
            name="Erfundener Fixposten", amount=PRAEMIE,
            interval=BudgetInterval.MONATLICH, kind="fix", category_id=kategorie_id,
        ))
        db.commit()
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert not a.abo.veraltet


def test_jahresfixposten_wird_auf_monat_umgerechnet(reihe: tuple[int, int]) -> None:
    """Ein Jahresbetrag darf nicht gegen einen Monatswert gehalten werden.

    Ohne Umrechnung wäre jeder jährlich erfasste Fixposten dauerhaft „veraltet",
    obwohl er stimmt — 9'324 im Jahr sind exakt 777 im Monat.
    """
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 10)
        db.add(ManualSubscription(
            name="Erfundener Fixposten", amount=Decimal("9324.00"),
            interval=BudgetInterval.JAEHRLICH, kind="fix", category_id=kategorie_id,
        ))
        db.commit()
        a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, reihen_id)))

    assert a.abo.hinterlegt == PRAEMIE
    assert not a.abo.veraltet


def test_vermoegensreihe_wird_nicht_verglichen() -> None:
    """Ein Altersguthaben hat keine Gegenbuchung — ein Abgleich wäre sinnlos.

    Würde es trotzdem verglichen, meldete die Seite dauerhaft „nichts gebucht"
    für etwas, das gar nie gebucht werden kann.
    """
    with _db() as db:
        pk = metrics.reihe_nach_slug(db, "pk_guthaben")
        assert pk is not None
        a = soll_ist.abgleich(db, metrics.verlauf(db, pk))

    assert not a.geprueft
    assert a.grund
    assert a.perioden == []


def test_reihe_ohne_kategorie_sagt_das_offen() -> None:
    """Der Lohn ist bewusst nicht verknüpft — brutto gegen netto wäre Unsinn.

    Wichtig ist, dass die Seite das SAGT. Ein stiller leerer Block sähe aus wie
    „alles in Ordnung".
    """
    with _db() as db:
        lohn = metrics.reihe_nach_slug(db, "lohn")
        assert lohn is not None
        assert lohn.category_id is None
        a = soll_ist.abgleich(db, metrics.verlauf(db, lohn))

    assert not a.geprueft
    assert "Kategorie" in a.grund


def test_quartalsreihe_meldet_keinen_veralteten_fixposten() -> None:
    """Ein Quartalswert gegen einen Monatsbetrag ist kein Vergleich.

    Strom wird quartalsweise abgerechnet, ein Fixposten läuft monatlich oder
    jährlich. Ohne Schranke verglich der Abgleich beides direkt: 159.60 für ein
    Quartal gegen 55.00 im Monat — und meldete dauerhaft „veraltet", obwohl
    nichts falsch war. Der Knopf daneben hätte dann den Quartalsbetrag in den
    monatlichen Posten geschrieben.

    Dass ein Posten EXISTIERT, bleibt die Meldung wert — nur über seinen Betrag
    sagt die Reihe nichts.
    """
    with _db() as db:
        strom = reihe_nach_slug(db, "strom")
        assert strom is not None
        assert strom.cadence.value == "quartalsweise"
        start = date(JAHR, 1, 1)
        setze_punkt(db, strom, start=start,
                    ende=metrics.periode_aus_takt(strom.cadence, start),
                    wert=Decimal("444.00"))
        db.add(ManualSubscription(
            name="Erfundener Fixposten", amount=Decimal("55.00"),
            interval=BudgetInterval.MONATLICH, kind="fix",
            category_id=strom.category_id,
        ))
        db.commit()
        strom_id = strom.id
    try:
        with _db() as db:
            a = soll_ist.abgleich(db, metrics.verlauf(db, db.get(MetricSeries, strom_id)))
        assert a.abo is not None
        assert a.abo.erfasst, "Dass der Posten da ist, gehört gemeldet"
        assert a.abo.erwartet is None, "Über den Betrag kann die Reihe nichts sagen"
        assert not a.abo.veraltet, "444 fürs Quartal gegen 55 im Monat ist kein Befund"
    finally:
        with _db() as db:
            db.query(ManualSubscription).filter(
                ManualSubscription.name == "Erfundener Fixposten"
            ).delete()
            db.query(MetricPoint).filter(MetricPoint.series_id == strom_id).delete()
            db.commit()


def test_abgleich_steht_wirklich_auf_der_seite(
    logged_in_client: TestClient, reihe: tuple[int, int]
) -> None:
    """Der Dienst rechnet — aber sieht man das Ergebnis auch?

    Der Grund für diesen Test: der Abgleich existierte eine Zeit lang als
    Dienst samt Tests, war aber an keinen Router und kein Template angebunden.
    Alles war grün, und auf der Seite stand nichts. Ein Test, der nur den Dienst
    prüft, kann das nicht bemerken — dieser hier ruft die Seite auf.
    """
    reihen_id, kategorie_id = reihe
    with _db() as db:
        _soll(db, reihen_id, 11)
        _buchung(db, kategorie_id, date(JAHR, 11, 4), "-700.00")
        db.add(ManualSubscription(
            name="Erfundener Fixposten", amount=Decimal("700.00"),
            interval=BudgetInterval.MONATLICH, kind="fix", category_id=kategorie_id,
        ))
        db.commit()

    html = logged_in_client.get("/verlaeufe").text

    assert "vl-abgleich" in html, "Der Abgleich fehlt komplett auf der Seite"
    # Die Abweichung (700 gebucht statt 777 verlangt) muss benannt sein — und
    # zwar so, dass die Zahl eine Einheit hat. „26 weichen ab" liess offen,
    # wovon 26 (Buchungen? Franken? Monate?), gemeldet .
    assert "anderer Betrag als im Beleg" in html
    # … und der veraltete Fixposten mit beiden Beträgen, benannt nach seiner
    # Wirkung: was das Budget rechnet, gegen das, was der Beleg sagt.
    assert "Budget rechnet mit" in html
    assert "777.00" in html
    # Zu jedem Befund muss auch der Weg dastehen, ihn zu prüfen.
    assert f"/transactions?category_id={kategorie_id}&amp;zeitraum=alles" in html, \
        "Kein Weg von der Meldung zu den Buchungen, die sie meint"


def test_reihe_ohne_abgleich_sagt_das_auf_der_seite(logged_in_client: TestClient) -> None:
    """Beim Lohn ist der fehlende Abgleich Absicht — und muss dastehen.

    Eine leere Stelle läse sich wie „alles in Ordnung". Genau deshalb schreibt
    die Seite den Grund aus, statt den Block wegzulassen.
    """
    with _db() as db:
        lohn = reihe_nach_slug(db, "lohn")
        assert lohn is not None
        start = date(JAHR, 1, 1)
        setze_punkt(
            db, lohn, start=start, ende=date(JAHR, 12, 31), wert=Decimal("99000.00")
        )
        db.commit()
        lohn_id = lohn.id

    try:
        html = logged_in_client.get("/verlaeufe").text
        assert "Kein Abgleich" in html
    finally:
        with _db() as db:
            db.query(MetricPoint).filter(MetricPoint.series_id == lohn_id).delete()
            db.commit()
