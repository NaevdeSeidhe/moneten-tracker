"""Aus einer Verlaufsreihe wird mit einem Klick ein wiederkehrender Posten.

Der Abgleich unter jedem Diagramm meldete bisher „kein Fixposten" oder
„Fixposten CHF 290.00, Beleg CHF 313.13" — und damit war Schluss. Die Zahl
stand da, übertragen musste man sie von Hand. Genau daran blieb es hängen:
„aktuell nur wohnung drin, dachte habe mehr erfasst".

Diese Tests halten die drei Stellen fest, an denen ein Knopf mehr schaden als
nützen kann:

1. **Der Betrag muss im Intervall des Postens stehen.** Ein Monatswert in einem
   Jahresabo ist ein Zwölftel des Wahren — und der Abgleich meldete danach
   dieselbe Abweichung weiter, nur grösser.
2. **Der Knopf darf nur dort stehen, wo die Route ihn annimmt.** Sonst führt er
   in einen 400er.
3. **Kein zweiter Posten je Kategorie.** Der Abgleich findet immer nur einen;
   ein doppelter zählte im Budget mit und wäre unsichtbar.

ALLE BETRÄGE HIER SIND ERFUNDEN. Die 313.13 ist absichtlich krumm, damit sie
nie mit einer echten Prämie verwechselt wird.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import (
    BudgetInterval,
    Category,
    ManagementType,
    ManualSubscription,
    MetricCadence,
    MetricKind,
    MetricSeries,
    MetricUnit,
)
from moneten.db.session import SessionLocal
from moneten.routers.metrics import haendler_schluessel, posten_art
from moneten.services import metrics, soll_ist
from moneten.services.subscriptions import _merchant_key

# Weit in der Zukunft, damit nichts mit Seed- oder anderen Testdaten kollidiert.
JAHR = 2033
MONATSWERT = Decimal("313.13")
JAHRESWERT = Decimal("3757.56")  # 12 × 313.13 — die Gegenprobe zur Umrechnung
ALTWERT = Decimal("290.00")


class Reihe(NamedTuple):
    """Was die Tests von der frisch angelegten Reihe brauchen."""

    slug: str
    id: int
    kategorie_id: int | None


@contextmanager
def _db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


@pytest.fixture
def baue():
    """Legt Reihen (mit eigener Kategorie) an und räumt sie restlos wieder weg.

    Eigene Kategorie je Reihe, nicht die geseedete: der Fixposten hängt an der
    Kategorie, und alle Tests teilen sich EINE Datenbank. Über eine gemeinsame
    Kategorie sähe ein Test den Posten des anderen — und zwar genau in dem
    Zweig, der prüft, dass es noch keinen gibt.
    """
    gebaut: list[Reihe] = []

    def _bauen(
        takt: MetricCadence = MetricCadence.MONATLICH,
        *,
        art: MetricKind = MetricKind.AUSGABE,
        mit_kategorie: bool = True,
        abo_kategorie: bool = False,
    ) -> Reihe:
        marke = uuid.uuid4().hex[:8]
        with _db() as db:
            kategorie_id = None
            if mit_kategorie:
                kat = Category(
                    name=f"Erfundene Kategorie {marke}",
                    management_type=ManagementType.DAUERAUFTRAG,
                    is_subscription=abo_kategorie,
                )
                db.add(kat)
                db.flush()
                kategorie_id = kat.id
            r = MetricSeries(
                slug=f"fixtest_{marke}",
                name=f"Erfundene Reihe {marke}",
                unit=MetricUnit.CHF,
                cadence=takt,
                kind=art,
                category_id=kategorie_id,
            )
            db.add(r)
            db.commit()
            reihe = Reihe(r.slug, r.id, r.category_id)
        gebaut.append(reihe)
        return reihe

    yield _bauen

    with _db() as db:
        for reihe in gebaut:
            if reihe.kategorie_id is not None:
                # Erst der Posten, dann die Kategorie: umgekehrt bliebe eine
                # Zeile mit ins Leere zeigender category_id stehen und der
                # nächste Seitenaufbau zöge sie in den Abgleich.
                for abo in db.scalars(
                    select(ManualSubscription).where(
                        ManualSubscription.category_id == reihe.kategorie_id
                    )
                ):
                    db.delete(abo)
            if (obj := db.get(MetricSeries, reihe.id)) is not None:
                db.delete(obj)  # nimmt die Punkte per cascade mit
            if reihe.kategorie_id is not None and (
                kat := db.get(Category, reihe.kategorie_id)
            ) is not None:
                db.delete(kat)
        db.commit()


def _punkt(reihe: Reihe, start: date, wert: Decimal) -> None:
    with _db() as db:
        r = db.get(MetricSeries, reihe.id)
        metrics.setze_punkt(
            db, r, start=start, ende=metrics.periode_aus_takt(r.cadence, start), wert=wert
        )
        db.commit()


def _abo(reihe: Reihe, betrag: Decimal, intervall: BudgetInterval) -> int:
    with _db() as db:
        m = ManualSubscription(
            name="Erfundener Fixposten", amount=betrag, interval=intervall,
            kind="fix", category_id=reihe.kategorie_id,
        )
        db.add(m)
        db.commit()
        return m.id


def _posten(reihe: Reihe) -> ManualSubscription | None:
    with _db() as db:
        return db.scalar(
            select(ManualSubscription).where(
                ManualSubscription.category_id == reihe.kategorie_id
            )
        )


def _abgleich(reihe: Reihe) -> soll_ist.Abgleich:
    with _db() as db:
        r = db.get(MetricSeries, reihe.id)
        return soll_ist.abgleich(db, metrics.verlauf(db, r))


def _hat_rumpf(antwort) -> bool:
    """Trägt eine Fehlerantwort gerendertes HTML?

    ``static/js/app.js`` swappt 4xx nur ein, wenn HTML mitkommt. Ohne Rumpf
    verschluckt HTMX die Meldung und die Oberfläche wirkt tot — dieser Fall ist
    in diesem Projekt schon einmal aufgetreten und hat einen eigenen Testlauf
    (``test_form_retention.py``) nach sich gezogen.
    """
    return (
        "text/html" in antwort.headers.get("content-type", "")
        and "verlaeufe-root" in antwort.text
    )


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------


def test_monatsreihe_wird_monatlicher_fixposten(logged_in_client: TestClient, baue) -> None:
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 3, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")
    assert antwort.status_code == 200, antwort.text

    m = _posten(reihe)
    assert m is not None
    assert m.amount == MONATSWERT
    assert m.interval == BudgetInterval.MONATLICH
    assert m.kind == "fix"
    assert m.category_id == reihe.kategorie_id
    assert m.name.startswith("Erfundene Reihe")


def test_betrag_kommt_vom_juengsten_punkt(logged_in_client: TestClient, baue) -> None:
    """Nicht der zuletzt EINGETRAGENE Wert zählt, sondern die jüngste Periode.

    Wer einen alten Monat nachträgt, korrigiert Vergangenheit — der Fixposten
    beschreibt aber, was ab jetzt läuft. Genau so bestimmt auch
    ``soll_ist.abgleich`` den Wert, gegen den es „veraltet" prüft; liefen die
    beiden Definitionen auseinander, mahnte der Abgleich den frisch angelegten
    Posten sofort wieder an.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 6, 1), MONATSWERT)
    _punkt(reihe, date(JAHR, 2, 1), Decimal("111.11"))  # älter, aber später erfasst

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert _posten(reihe).amount == MONATSWERT


def test_jahresreihe_wird_jaehrlicher_fixposten(logged_in_client: TestClient, baue) -> None:
    """Ein Jahresbetrag bleibt ein Jahresbetrag — keine stille Division."""
    reihe = baue(MetricCadence.JAEHRLICH)
    _punkt(reihe, date(JAHR, 1, 1), JAHRESWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    m = _posten(reihe)
    assert m.amount == JAHRESWERT
    assert m.interval == BudgetInterval.JAEHRLICH


def test_frischer_posten_gilt_nicht_als_veraltet(logged_in_client: TestClient, baue) -> None:
    """Die Probe aufs Ganze: der Abgleich muss danach schweigen.

    Ein Knopf, dessen Ergebnis dieselbe Seite im nächsten Atemzug anmahnt, wäre
    schlechter als kein Knopf.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 4, 1), MONATSWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    abo = _abgleich(reihe).abo
    assert abo.erfasst
    assert not abo.veraltet


def test_quartalsreihe_bekommt_keinen_fixposten(logged_in_client: TestClient, baue) -> None:
    """Quartalsweise passt auf kein Budget-Intervall — und wird nicht geraten.

    ÷3 legte eine Zahl in den Posten, die auf keiner Stromrechnung steht; ×4
    als Jahresposten würde von ``soll_ist`` sofort als veraltet gemeldet, weil
    der Abgleich bei Quartalsreihen ungerechnet vergleicht.
    """
    reihe = baue(MetricCadence.QUARTALSWEISE)
    _punkt(reihe, date(JAHR, 1, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 400
    assert _hat_rumpf(antwort)
    assert _posten(reihe) is None


def test_unregelmaessige_reihe_bekommt_keinen_fixposten(
    logged_in_client: TestClient, baue
) -> None:
    """Bei „unregelmässig" sagt der Takt nichts darüber, wofür der Wert gilt."""
    reihe = baue(MetricCadence.UNREGELMAESSIG)
    _punkt(reihe, date(JAHR, 5, 17), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 400
    assert _hat_rumpf(antwort)
    assert _posten(reihe) is None


def test_einnahmereihe_wird_abgewiesen(logged_in_client: TestClient, baue) -> None:
    """Ein Fixposten ist ein ABGANG. Ein Lohn als Fixposten wäre eine Ausgabe,
    die es nicht gibt — und würde im Budget als solche mitgezählt."""
    reihe = baue(MetricCadence.MONATLICH, art=MetricKind.EINNAHME)
    _punkt(reihe, date(JAHR, 7, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 400
    assert _hat_rumpf(antwort)
    assert _posten(reihe) is None


def test_reihe_ohne_kategorie_wird_abgewiesen(logged_in_client: TestClient, baue) -> None:
    """Ohne Kategorie fände der Abgleich den Posten nie wieder — er suchte ins Leere."""
    reihe = baue(MetricCadence.MONATLICH, mit_kategorie=False)
    _punkt(reihe, date(JAHR, 8, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 400
    assert _hat_rumpf(antwort)


def test_reihe_ohne_punkte_wird_abgewiesen(logged_in_client: TestClient, baue) -> None:
    reihe = baue(MetricCadence.MONATLICH)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 400
    assert _hat_rumpf(antwort)
    assert _posten(reihe) is None


def test_unbekannte_reihe_meldet_404_mit_rumpf(logged_in_client: TestClient) -> None:
    antwort = logged_in_client.post("/verlaeufe/gibt-es-nicht/fixposten")

    assert antwort.status_code == 404
    assert _hat_rumpf(antwort)


def test_zweiter_fixposten_wird_abgewiesen(logged_in_client: TestClient, baue) -> None:
    """Zweimal getippt ist am Handy der Normalfall.

    Ein zweiter Posten derselben Kategorie zählte im Budget doppelt mit, und
    der Abgleich zeigte ihn nicht — der sieht immer nur einen.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 9, 1), MONATSWERT)

    erste = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")
    zweite = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert erste.status_code == 200
    assert zweite.status_code == 409
    assert _hat_rumpf(zweite)
    with _db() as db:
        anzahl = len(list(db.scalars(
            select(ManualSubscription).where(
                ManualSubscription.category_id == reihe.kategorie_id
            )
        )))
    assert anzahl == 1


# ---------------------------------------------------------------------------
# Betrag übernehmen
# ---------------------------------------------------------------------------


def test_veralteter_betrag_wird_uebernommen(logged_in_client: TestClient, baue) -> None:
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 10, 1), MONATSWERT)
    _abo(reihe, ALTWERT, BudgetInterval.MONATLICH)
    assert _abgleich(reihe).abo.veraltet, "Vorbedingung: der Posten muss veraltet sein"

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    assert antwort.status_code == 200, antwort.text
    assert _posten(reihe).amount == MONATSWERT
    assert not _abgleich(reihe).abo.veraltet


def test_jahresposten_bekommt_den_monatswert_mal_zwoelf(
    logged_in_client: TestClient, baue
) -> None:
    """Der teuerste Fehler, den dieser Knopf machen könnte.

    ``soll_ist`` vergleicht einen Jahresposten gegen eine Monatsreihe, indem es
    den Posten durch zwölf teilt. Schriebe der Knopf den Monatswert
    ungerechnet hinein, stünden im Budget statt 3'757.56 nur 313.13 im Jahr —
    und der Abgleich meldete danach dieselbe Abweichung weiter.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 11, 1), MONATSWERT)
    _abo(reihe, Decimal("2000.00"), BudgetInterval.JAEHRLICH)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    m = _posten(reihe)
    assert m.amount == JAHRESWERT
    assert m.interval == BudgetInterval.JAEHRLICH
    assert not _abgleich(reihe).abo.veraltet


def test_monatsposten_bei_jahresreihe_bekommt_das_zwoelftel(
    logged_in_client: TestClient, baue
) -> None:
    """Die Gegenrichtung — inklusive der Rundung auf Rappen.

    3'757.56 / 12 geht glatt auf; die Rundung bleibt in jedem Fall unter
    ``soll_ist.ABO_TOLERANZ``, sonst gälte der Posten sofort wieder als
    veraltet.
    """
    reihe = baue(MetricCadence.JAEHRLICH)
    _punkt(reihe, date(JAHR, 1, 1), JAHRESWERT)
    _abo(reihe, Decimal("200.00"), BudgetInterval.MONATLICH)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    m = _posten(reihe)
    assert m.amount == MONATSWERT
    assert m.interval == BudgetInterval.MONATLICH
    assert not _abgleich(reihe).abo.veraltet


def test_uebernehmen_laesst_name_und_intervall_stehen(
    logged_in_client: TestClient, baue
) -> None:
    """Der Befund lautet „der Betrag ist veraltet", nicht „der Posten ist falsch"."""
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 2, 1), MONATSWERT)
    _abo(reihe, ALTWERT, BudgetInterval.MONATLICH)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    m = _posten(reihe)
    assert m.name == "Erfundener Fixposten"
    assert m.interval == BudgetInterval.MONATLICH
    assert m.kind == "fix"


def test_uebernehmen_ist_wiederholbar(logged_in_client: TestClient, baue) -> None:
    """Der zweite Tipp ist kein Fehler, sondern nichts zu tun.

    Ein 4xx darauf wäre eine Fehlermeldung für einen Erfolg — und am Handy
    passiert der zweite Tipp regelmässig.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 3, 1), MONATSWERT)
    _abo(reihe, ALTWERT, BudgetInterval.MONATLICH)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")
    zweite = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    assert zweite.status_code == 200
    assert _posten(reihe).amount == MONATSWERT


def test_uebernehmen_ohne_fixposten_meldet_404(logged_in_client: TestClient, baue) -> None:
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 4, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten/betrag")

    assert antwort.status_code == 404
    assert _hat_rumpf(antwort)


# ---------------------------------------------------------------------------
# Steht es auch auf der Seite?
# ---------------------------------------------------------------------------
#
# Der Grund für diese drei: der Soll/Ist-Abgleich existierte in diesem Projekt
# eine Zeit lang als Dienst samt grüner Tests, war aber an kein Template
# angebunden. Alles war grün, und auf der Seite stand nichts.


def test_knopf_steht_auf_der_seite_und_verschwindet_danach(
    logged_in_client: TestClient, baue
) -> None:
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 5, 1), MONATSWERT)

    vorher = logged_in_client.get("/verlaeufe").text
    assert f'action="/verlaeufe/{reihe.slug}/fixposten"' in vorher
    # Der Knopf nennt Betrag UND Takt: „Fixposten anlegen" war das Fachwort für
    # genau das, wonach der Nutzer  fragte („was heisst fixposten
    # anlegen?"). Wer liest, was der Knopf tut, muss ihn nicht erklärt bekommen.
    assert f"CHF {MONATSWERT} monatlich einplanen" in vorher,         "Der Knopf sagt nicht, welcher Betrag in welchem Takt eingeplant wird"

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")
    nachher = logged_in_client.get("/verlaeufe").text

    # Angelegt heisst erledigt: der Knopf hat keine Aufgabe mehr, und stünde er
    # weiter da, führte er in den 409er.
    assert f'action="/verlaeufe/{reihe.slug}/fixposten"' not in nachher


def test_veralteter_posten_zeigt_den_uebernehmen_knopf(
    logged_in_client: TestClient, baue
) -> None:
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 6, 1), MONATSWERT)
    _abo(reihe, ALTWERT, BudgetInterval.MONATLICH)

    html = logged_in_client.get("/verlaeufe").text

    assert f'action="/verlaeufe/{reihe.slug}/fixposten/betrag"' in html
    assert f"Budget auf CHF {MONATSWERT} setzen" in html,         "Der Knopf sagt nicht, worauf er das Budget setzt"
    # Beide Zahlen müssen daneben stehen, sonst weiss niemand, was übernommen wird.
    assert "290.00" in html
    assert "313.13" in html


def test_quartalsreihe_nennt_den_grund_statt_eines_knopfes(
    logged_in_client: TestClient, baue
) -> None:
    """Eine leere Stelle sähe nach einem Fehler aus — der Grund muss dastehen."""
    reihe = baue(MetricCadence.QUARTALSWEISE)
    _punkt(reihe, date(JAHR, 7, 1), MONATSWERT)

    html = logged_in_client.get("/verlaeufe").text

    assert f'action="/verlaeufe/{reihe.slug}/fixposten"' not in html
    assert "passt auf kein Budget-Intervall" in html,         "Die leere Stelle nennt den Grund nicht"
    assert "von Hand anlegen" in html


# ---------------------------------------------------------------------------
# Abo statt Fixkosten, und die Verbindung zu den echten Buchungen
# ---------------------------------------------------------------------------
#
# Gemeldet als „ist ein abo, die abokosten sollen unter abos erscheinen". Zwei
# Dinge fehlten dafür, und beide fallen ohne Test nicht auf:
#
#   * Der Posten landete immer unter „Fixkosten" (``kind="fix"``), egal was er
#     war. Die Handyrechnung stand damit im falschen Topf.
#   * Er trug keinen Händler-Schlüssel. Die Auto-Erkennung sah dieselben
#     Bankbuchungen weiterhin als eigenes Abo — der Betrag stand zweimal auf der
#     Seite und zählte zweimal im Monatstotal.


def _positionen(reihe: Reihe, start: date, wert: Decimal) -> None:
    """Ein Punkt MIT Aufschlüsselung — erfundene Positionen, erfundene Beträge."""
    with _db() as db:
        r = db.get(MetricSeries, reihe.id)
        metrics.setze_punkt(
            db, r, start=start, ende=metrics.periode_aus_takt(r.cadence, start),
            wert=wert,
            extras={"pos:Abonnement Basis": "250.00", "pos:Zusatzoption": "63.13"},
        )
        db.commit()


def test_aufgeschlüsselte_monatsreihe_wird_ein_abo(
    logged_in_client: TestClient, baue
) -> None:
    """Eine Rechnung mit Abonnements-, Options- und Rabattzeilen IST ein Abo.

    Die Kategorie allein sagt es nicht: „Handy-Abo" trägt im Seed
    ``is_subscription=False``, und danach entschied der Knopf.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _positionen(reihe, date(JAHR, 3, 1), MONATSWERT)

    antwort = logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert antwort.status_code == 200, antwort.text
    assert _posten(reihe).kind == "abo"


def test_reihe_ohne_positionen_bleibt_ein_fixposten(
    logged_in_client: TestClient, baue
) -> None:
    """Miete und Prämie sind keine Abos — die Unterscheidung muss halten."""
    reihe = baue(MetricCadence.MONATLICH)
    _punkt(reihe, date(JAHR, 4, 1), MONATSWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert _posten(reihe).kind == "fix"


def test_jahresreihe_mit_positionen_bleibt_ein_fixposten(
    logged_in_client: TestClient, baue
) -> None:
    """Eine Jahrespolice mit Positionen ist keine monatliche Leistung."""
    reihe = baue(MetricCadence.JAEHRLICH)
    _positionen(reihe, date(JAHR, 1, 1), JAHRESWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert _posten(reihe).kind == "fix"


def test_abo_kategorie_entscheidet_auch_ohne_positionen(
    logged_in_client: TestClient, baue
) -> None:
    """Dieselbe Regel wie auf der Abo-Seite: das Flag der Kategorie zuerst."""
    reihe = baue(MetricCadence.MONATLICH, abo_kategorie=True)
    _punkt(reihe, date(JAHR, 5, 1), MONATSWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    assert _posten(reihe).kind == "abo"


def test_die_art_folgt_derselben_regel_wie_die_abo_seite() -> None:
    """Zwei Regeln für dieselbe Frage liefen auseinander — dann stünde
    derselbe Händler je nach Weg mal unter „Abos" und mal unter „Fixkosten"."""
    from moneten.routers.subscriptions import _detected_kind

    class _Erkannt:
        def __init__(self, kategorie):
            self.category = kategorie

    for flag in (True, False):
        kategorie = Category(name="Erfunden", management_type=ManagementType.DAUERAUFTRAG,
                             is_subscription=flag)
        reihe = MetricSeries(slug="x", name="x", unit=MetricUnit.CHF,
                             cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE)
        assert posten_art(kategorie, reihe, False) == _detected_kind(_Erkannt(kategorie))


def test_der_posten_hängt_an_den_echten_buchungen(
    logged_in_client: TestClient, baue
) -> None:
    """Ohne ``match_keyword`` stand die Rechnung zweimal auf ``/abos``.

    Der Schlüssel entsteht aus dem Namen der Reihe — normalisiert mit genau der
    Funktion, die die Abo-Erkennung auf jeden Buchungstext anwendet.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _positionen(reihe, date(JAHR, 6, 1), MONATSWERT)

    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    posten = _posten(reihe)
    assert posten.match_keyword, "Der Posten hängt an keiner Buchung"
    assert posten.match_keyword == _merchant_key(posten.name)


def test_ein_namensloser_schluessel_wird_nicht_gesetzt() -> None:
    """Bleibt vom Namen nichts übrig, ist ein leerer Schlüssel schlimmer als
    keiner: er überspringt nichts und steht als Rätsel im Abo-Formular."""
    reihe = MetricSeries(slug="x", name="AB", unit=MetricUnit.CHF,
                         cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE)
    assert haendler_schluessel(reihe) is None


def test_die_reihe_taucht_danach_unter_abos_auf(
    logged_in_client: TestClient, baue
) -> None:
    """Die Probe aufs Ganze — und der Grund für diesen Abschnitt.

    Der Soll/Ist-Abgleich existierte in diesem Projekt eine Zeit lang als Dienst
    samt grüner Tests, war aber an kein Template angebunden.
    """
    reihe = baue(MetricCadence.MONATLICH)
    _positionen(reihe, date(JAHR, 7, 1), MONATSWERT)
    logged_in_client.post(f"/verlaeufe/{reihe.slug}/fixposten")

    html = logged_in_client.get("/subscriptions").text

    abo_teil = html.split("Wiederkehrende Zahlungen")[0]
    assert _posten(reihe).name in abo_teil, "Der Posten steht nicht im Abo-Topf"
