"""Was auf der Karte einer Verlaufsreihe DAUERHAFT sichtbar ist.

Die Seite wird zu 90 % am Handy gelesen. Dort entscheidet nicht, wie viel eine
Karte enthält, sondern wie weit man scrollen muss, bis die Zahl dasteht, wegen
der man sie geöffnet hat.

Zwei Stellen kosteten genau das, und beide lassen sich still zurückholen — ein
``<p>`` mehr im Kopf fällt in keinem Test auf:

* Die **Reihenbeschreibung** (``MetricSeries.note``) stand zwischen Überschrift
  und Leitzahl. Gemessen bei 375px schob sie die Leitzahl von 85px auf 124px
  hinunter, bei der längsten Notiz auf 229px. Gesagt hat sie dabei, was die
  Überschrift schon sagt.
* Der Satz der **leeren Reihe** wies auf das Formular direkt darunter hin, das
  bei einer leeren Reihe ohnehin aufgeklappt dasteht und „Ersten Wert eintragen"
  heisst.

Gelöscht ist nichts: die Notiz steht am Kopf der Werteliste, bei einer leeren
Reihe weiterhin auf der Karte — dort gibt es die Liste noch nicht.

ALLE DATEN HIER SIND ERFUNDEN.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from moneten.db.models import (
    MetricCadence,
    MetricKind,
    MetricPoint,
    MetricSeries,
    MetricUnit,
)
from moneten.db.session import SessionLocal

# Weit in der Zukunft, damit nichts mit Seed- oder anderen Testdaten kollidiert.
JAHR = 2039
NOTIZ = "Erfundene Herkunftsangabe laut erfundenem Ausweis."


def _karte(html: str, slug: str) -> str:
    karten = html.split('<article class="card vl-karte">')
    treffer = [k for k in karten if f"/verlaeufe/{slug}/punkt" in k]
    assert len(treffer) == 1, f"{len(treffer)} Karten für {slug}"
    return treffer[0]


def _reihe(*, mit_werten: bool) -> str:
    marke = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        r = MetricSeries(
            slug=f"notiz_{marke}", name=f"Erfundene Reihe {marke}",
            unit=MetricUnit.CHF, cadence=MetricCadence.JAEHRLICH,
            kind=MetricKind.AUSGABE, note=NOTIZ,
        )
        db.add(r)
        db.flush()
        if mit_werten:
            for i, wert in enumerate(("311.00", "329.50", "348.00")):
                db.add(MetricPoint(
                    series_id=r.id, period_start=date(JAHR + i, 1, 1),
                    period_end=date(JAHR + i, 12, 31), value=Decimal(wert),
                    source="erfundener-beleg.pdf",
                ))
        db.commit()
        return r.slug


@pytest.fixture
def reihe_mit_werten():
    slug = _reihe(mit_werten=True)
    yield slug
    _weg(slug)


@pytest.fixture
def leere_reihe():
    slug = _reihe(mit_werten=False)
    yield slug
    _weg(slug)


def _weg(slug: str) -> None:
    with SessionLocal() as db:
        from sqlalchemy import select

        if (obj := db.scalar(select(MetricSeries).where(MetricSeries.slug == slug))):
            db.delete(obj)
        db.commit()


def test_die_beschreibung_steht_nicht_zwischen_namen_und_leitzahl(
    logged_in_client: TestClient, reihe_mit_werten
) -> None:
    """Sie schob die Leitzahl bei 375px um 39px nach unten und sagte dabei, was
    die Überschrift schon sagt."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, reihe_mit_werten)
    kopf = karte[:karte.index('class="metric-value"')]
    assert NOTIZ not in kopf, (
        "Die Reihenbeschreibung steht wieder über der Leitzahl"
    )


def test_die_beschreibung_bleibt_in_der_werteliste_erreichbar(
    logged_in_client: TestClient, reihe_mit_werten
) -> None:
    """Verschoben, nicht gelöscht: sie sagt, WOHER die Werte stammen — dieselbe
    Frage, die jede Zeile der Liste für sich beantwortet. Ohne diese Prüfung
    verschwände die Angabe beim nächsten Aufräumen spurlos."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, reihe_mit_werten)
    liste = re.search(r'<details class="disclosure vl-liste">(.*?)</details>',
                      karte, re.S)
    assert liste, "Keine Werteliste auf der Karte"
    assert NOTIZ in liste.group(1)


def test_die_leere_reihe_zeigt_ihre_beschreibung_weiterhin(
    logged_in_client: TestClient, leere_reihe
) -> None:
    """Ohne Werte gibt es keine Werteliste — und damit keinen anderen Ort. Dann
    ist die Notiz das Einzige, was die Reihe überhaupt beschreibt."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, leere_reihe)
    assert NOTIZ in karte


def test_die_leere_reihe_wiederholt_nicht_die_aufschrift_ihres_formulars(
    logged_in_client: TestClient, leere_reihe
) -> None:
    """„Trag den ersten unten ein" stand über einem Formular, das bei leerer
    Reihe aufgeklappt dasteht und dessen Griff „Ersten Wert eintragen" heisst.
    Der erwartete TAKT bleibt: den sagt das Formular nicht."""
    karte = _karte(logged_in_client.get("/verlaeufe").text, leere_reihe)
    leer = re.search(r'<p class="vl-leer">(.*?)</p>', karte, re.S)
    assert leer, "Kein Hinweis auf der leeren Karte"
    text = " ".join(leer.group(1).split())
    assert "Trag den ersten" not in text
    assert "Noch kein Wert erfasst." in text
    assert "je Jahr" in text, "Der erwartete Takt fehlt"
    assert "Ersten Wert eintragen" in karte, "Vorbedingung: der Griff heisst so"
