"""Was für jemanden falsch war, der nicht der Autor ist.

Die App war für genau eine Person gebaut, und das sah man an fünf Stellen. Keine
davon war ein Fehler im Sinne von „stürzt ab" — alle fünf standen einfach im
Weg, wenn jemand anderes die App benutzt:

* Acht Vorgabe-Kategorien kamen nach jedem Neustart zurück, wenn man sie löschte.
  Wer sie umbenannte, hatte sie doppelt — und damit zwei Töpfe für dieselbe
  Sache, was jede Auswertung verfälscht.
* Verlaufsreihen liessen sich nicht loswerden. Sieben der zwölf sind
  schweizspezifisch, und eine leere Reihe klappt ihr Formular von selbst auf.
* Der Anzeigename stand nur im Seed. Eine frische Installation begrüsste ihren
  neuen Besitzer dauerhaft mit „Guten Abend, Ich".
* Die Starter-Regeln konnten auf eine Oberkategorie oder eine archivierte
  Kategorie zeigen — Buchungen landeten dann still an der falschen Stelle.
* Die Datenbank konnte unbemerkt ausserhalb des Projekts entstehen, weil
  ``.env.example`` einen Pfad setzt, der im Container gilt.

Alle Daten hier sind erfunden.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as OrmSession

from moneten.db.models import (
    Base,
    Category,
    CategoryRule,
    ManagementType,
    MetricCadence,
    MetricKind,
    MetricPoint,
    MetricSeries,
    MetricUnit,
    SeedMarke,
    User,
)
from moneten.db.seeds import _EXTRA_CATEGORIES, ensure_extra_categories, ensure_metric_series
from moneten.db.session import SessionLocal


# ---------------------------------------------------------------------------
# Vorgabe-Kategorien: einmal anbieten, dann in Ruhe lassen
# ---------------------------------------------------------------------------
def _erste_vorgabe() -> tuple[str, str]:
    """Schlüssel und Name der ersten nachgelieferten Kategorie."""
    key, name = _EXTRA_CATEGORIES[0][0], _EXTRA_CATEGORIES[0][1]
    return key, name


def test_geloeschte_vorgabe_kommt_nicht_zurueck() -> None:
    """Der gemessene Fall: löschen, neu starten — und sie stand wieder da.

    Der Seed lief bei jedem Start und prüfte auf den NAMEN. Jetzt entscheidet
    eine Merkung, die das Löschen der Kategorie überlebt.
    """
    key, name = _erste_vorgabe()
    with SessionLocal() as db:
        ensure_extra_categories(db)  # Ausgangslage: sie ist da und gemerkt
        cat = db.scalar(select(Category).where(Category.name == name))
        assert cat is not None, "Vorgabe wurde gar nicht erst angelegt"
        db.delete(cat)
        db.commit()

        ensure_extra_categories(db)  # „Neustart"
        assert db.scalar(select(Category).where(Category.name == name)) is None, (
            f"Die Vorgabe {name!r} ist nach dem nächsten Start wieder da"
        )
        assert db.scalar(select(SeedMarke).where(SeedMarke.schluessel == key)) is not None


def test_umbenannte_vorgabe_wird_nicht_verdoppelt() -> None:
    """Der teurere Fall: umbenennen erzeugte einen zweiten, leeren Topf.

    Beide sahen in der Kategorie-Auswahl gleich echt aus. Ab da konnten Buchungen
    für dieselbe Sache auf zwei Töpfe laufen — falsche Zahlen in jeder
    Auswertung, ohne dass irgendwo etwas fehlschlägt.
    """
    key, name = _EXTRA_CATEGORIES[1][0], _EXTRA_CATEGORIES[1][1]
    eigener_name = "Naschzeug (umbenannt)"
    with SessionLocal() as db:
        ensure_extra_categories(db)
        cat = db.scalar(select(Category).where(Category.name == name))
        if cat is None:  # in einem vorherigen Test gelöscht — dann neu anlegen
            db.execute(SeedMarke.__table__.delete().where(SeedMarke.schluessel == key))
            db.commit()
            ensure_extra_categories(db)
            cat = db.scalar(select(Category).where(Category.name == name))
        assert cat is not None
        cat.name = eigener_name
        db.add(cat)
        db.commit()

        ensure_extra_categories(db)
        assert db.scalar(select(Category).where(Category.name == name)) is None, (
            "Die Vorgabe wurde neben der umbenannten noch einmal angelegt"
        )
        assert db.scalar(select(Category).where(Category.name == eigener_name)) is not None


def test_ohne_merkung_wird_die_vorgabe_angelegt() -> None:
    """Gegenprobe: die Merkung darf das Anlegen nicht grundsätzlich verhindern."""
    key, name = _EXTRA_CATEGORIES[2][0], _EXTRA_CATEGORIES[2][1]
    with SessionLocal() as db:
        ensure_extra_categories(db)
        cat = db.scalar(select(Category).where(Category.name == name))
        if cat is not None:
            db.delete(cat)
        db.execute(SeedMarke.__table__.delete().where(SeedMarke.schluessel == key))
        db.commit()

        ensure_extra_categories(db)
        assert db.scalar(select(Category).where(Category.name == name)) is not None, (
            "Ohne Merkung müsste die Vorgabe entstehen"
        )


# ---------------------------------------------------------------------------
# Verlaufsreihen: ausblenden statt in der Datenbank herumschreiben
# ---------------------------------------------------------------------------
@pytest.fixture
def reihe() -> str:
    """Eine erfundene Reihe mit einem Wert — sichtbar, egal was vorher lief.

    Die Testdatenbank lebt über die ganze Sitzung, also legt die Fixture nicht
    blind an: beim zweiten Test wäre der Slug schon vergeben. Stattdessen holt
    sie die Reihe und stellt den Ausgangszustand her.
    """
    with SessionLocal() as db:
        r = db.scalar(select(MetricSeries).where(MetricSeries.slug == "fremd-testreihe"))
        if r is None:
            r = MetricSeries(
                slug="fremd-testreihe", name="Fremd-Testreihe", unit=MetricUnit.CHF,
                cadence=MetricCadence.MONATLICH, kind=MetricKind.AUSGABE, sort_order=980,
            )
            db.add(r)
            db.flush()
        r.archived = False
        if not db.scalar(select(func.count(MetricPoint.id)).where(MetricPoint.series_id == r.id)):
            db.add(MetricPoint(
                series_id=r.id, period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
                value=Decimal("123.45"), source="manuell",
            ))
        db.commit()
        return r.slug


def test_reihe_laesst_sich_ausblenden_und_zurueckholen(
    logged_in_client: TestClient, reihe: str
) -> None:
    """Sieben der zwölf geseedeten Reihen sind schweizspezifisch.

    Vorher gab es keinen Weg, eine davon loszuwerden: ``archived`` wurde gelesen,
    aber nie geschrieben, und keine Route rührte die Reihe an.
    """
    seite = logged_in_client.get("/verlaeufe").text
    assert "Fremd-Testreihe" in seite

    antwort = logged_in_client.post(f"/verlaeufe/{reihe}/archiv")
    assert antwort.status_code == 200, antwort.status_code
    with SessionLocal() as db:
        r = db.scalar(select(MetricSeries).where(MetricSeries.slug == reihe))
        assert r.archived is True
        # Die Werte bleiben — daran hängt der ganze Unterschied zum Löschen.
        assert db.scalar(
            select(func.count(MetricPoint.id)).where(MetricPoint.series_id == r.id)
        ) == 1

    zurueck = logged_in_client.post(f"/verlaeufe/{reihe}/archiv", data={"zurueck": "1"})
    assert zurueck.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(MetricSeries).where(MetricSeries.slug == reihe)).archived is False


def test_ausgeblendete_reihe_wird_vom_seed_nicht_wiederbelebt(
    logged_in_client: TestClient, reihe: str
) -> None:
    """Sonst wäre das Ausblenden bis zum nächsten Neustart wirksam."""
    logged_in_client.post(f"/verlaeufe/{reihe}/archiv")
    with SessionLocal() as db:
        ensure_metric_series(db)
        r = db.scalar(select(MetricSeries).where(MetricSeries.slug == reihe))
        assert r is not None and r.archived is True, "Der Seed hat die Reihe zurückgeholt"


def test_unbekannte_reihe_gibt_404_und_keine_leere_seite(logged_in_client: TestClient) -> None:
    """Zweimal getippt ist am Handy der Normalfall — die Antwort muss trotzdem
    eine gerenderte Seite sein, sonst wirkt die Oberfläche tot."""
    antwort = logged_in_client.post("/verlaeufe/gibt-es-nicht/archiv")
    assert antwort.status_code == 404
    assert "Diese Reihe gibt es nicht" in antwort.text


# ---------------------------------------------------------------------------
# Anzeigename
# ---------------------------------------------------------------------------
def test_name_laesst_sich_aendern(logged_in_client: TestClient) -> None:
    """„Guten Abend, Ich" — der Name stand nur im Seed und nirgends zum Ändern."""
    antwort = logged_in_client.post("/settings/name", data={"name": "  Erfundene Person  "})
    assert antwort.status_code == 204
    with SessionLocal() as db:
        assert db.get(User, 1).name == "Erfundene Person"

    # Und er steht danach auf dem Dashboard.
    assert "Erfundene Person" in logged_in_client.get("/").text


def test_leerer_name_laesst_den_alten_stehen(logged_in_client: TestClient) -> None:
    """Ein versehentlich geleertes Feld darf die Begrüssung nicht abschneiden."""
    logged_in_client.post("/settings/name", data={"name": "Vorher"})
    logged_in_client.post("/settings/name", data={"name": "   "})
    with SessionLocal() as db:
        assert db.get(User, 1).name == "Vorher"


def test_zu_langer_name_wird_gekuerzt(logged_in_client: TestClient) -> None:
    """Die Spalte ist 80 Zeichen breit — ohne Kürzen fiele das erst in der
    Datenbank auf, also mit einem 500er statt einer Antwort."""
    antwort = logged_in_client.post("/settings/name", data={"name": "X" * 200})
    assert antwort.status_code == 204
    with SessionLocal() as db:
        assert len(db.get(User, 1).name) == 80


def test_die_einstellungsseite_zeigt_ein_feld_und_keinen_text(
    logged_in_client: TestClient
) -> None:
    """Der Name stand als Text genau dort, wo man ihn ändern würde."""
    seite = logged_in_client.get("/settings").text
    assert 'hx-post="/settings/name"' in seite, "Kein Eingabefeld für den Namen"


# ---------------------------------------------------------------------------
# Starter-Regeln zeigen auf Unterkategorien
# ---------------------------------------------------------------------------
def _frische_db() -> OrmSession:
    """Eigene DB im Speicher — die Starter-Regeln laufen nur auf einer leeren."""
    motor = create_engine("sqlite://")
    Base.metadata.create_all(motor)
    return OrmSession(motor)


def test_starter_regel_zeigt_auf_die_unterkategorie() -> None:
    """Namensgleichheit zwischen Ober- und Unterkategorie entschied die Reihenfolge.

    Gebucht wird auf der Unterkategorie; die Oberkategorie ist eine Klammer.
    Zeigte eine Regel dorthin, landeten Buchungen still an einer Stelle, an der
    keine landen soll — und niemand sieht es.
    """
    from moneten.services.categorization import _STARTER, seed_starter_rules

    ziel_name = _STARTER[0][1]
    with _frische_db() as db:
        oben = Category(name="Erfundene Klammer", management_type=ManagementType.BARGELD)
        db.add(oben)
        db.flush()
        unten = Category(name=ziel_name, parent_id=oben.id,
                         management_type=ManagementType.BARGELD)
        db.add(unten)
        db.flush()
        # Die eigene OBERkategorie mit demselben Namen entsteht ZULETZT — sie
        # bekommt damit die höhere ID und gewinnt in einer ungefilterten
        # ``{name: id}``-Abbildung. Ohne diese Reihenfolge bestand der Test auch
        # ohne den Filter: die Zuordnung traf zufällig die richtige Zeile, und
        # der Test hätte nichts festgehalten (per Mutation nachgemessen).
        koeder = Category(name=ziel_name, management_type=ManagementType.BARGELD)
        db.add(koeder)
        db.commit()

        seed_starter_rules(db)
        regel = db.scalar(select(CategoryRule).where(CategoryRule.category_id == unten.id))
        assert regel is not None, "Keine Regel zeigt auf die Unterkategorie"
        assert db.scalar(
            select(func.count(CategoryRule.id)).where(CategoryRule.category_id == koeder.id)
        ) == 0, "Eine Regel zeigt auf die Oberkategorie"


def test_starter_regel_meidet_archivierte_kategorien() -> None:
    """Eine archivierte Kategorie taucht in keiner Auswahl mehr auf — als
    Regelziel wäre sie ein Ort, den der Nutzer nicht mehr sieht."""
    from moneten.services.categorization import _STARTER, seed_starter_rules

    ziel_name = _STARTER[0][1]
    with _frische_db() as db:
        oben = Category(name="Erfundene Klammer", management_type=ManagementType.BARGELD)
        db.add(oben)
        db.flush()
        db.add(Category(name=ziel_name, parent_id=oben.id, is_archived=True,
                        management_type=ManagementType.BARGELD))
        db.commit()

        seed_starter_rules(db)
        assert db.scalar(select(func.count(CategoryRule.id))) == 0, (
            "Es entstand eine Regel auf eine archivierte Kategorie"
        )


# ---------------------------------------------------------------------------
# Die Datenbank landet nicht unbemerkt ausserhalb des Projekts
# ---------------------------------------------------------------------------
def test_warnung_wenn_die_datenbank_weit_weg_angelegt_wird(tmp_path, caplog) -> None:
    """Der gemessene Fall: ``.env.example`` unverändert für einen lokalen Lauf.

    Der Pfad darin gilt IM CONTAINER. Unter Windows entsteht dann ein ``/app/data``
    in der Wurzel des Laufwerks, das Projekt-``data/`` bleibt leer — und wer das
    später merkt, sucht seine Buchungen nicht mehr dort.
    """
    from moneten.config import Settings

    weit_weg = tmp_path / "gibt-es-noch-nicht" / "moneten.db"
    with caplog.at_level(logging.WARNING, logger="moneten.config"):
        Settings(
            database_url=f"sqlite:///{weit_weg.as_posix()}",
            secret_key="probe-schluessel-lang-genug-fuer-den-test",
            initial_pin="424242",
        )
    meldungen = " ".join(r.getMessage() for r in caplog.records)
    assert "ausserhalb des Projekts" in meldungen, f"keine Warnung: {meldungen!r}"


def test_keine_warnung_wenn_der_ordner_schon_existiert(tmp_path, caplog) -> None:
    """Wer seine Daten bewusst auf ein anderes Volume legt, hat den Ordner längst.

    Ohne diese Grenze feuerte die Warnung bei jedem Docker-Start: ``/app/data``
    liegt dort legitim ausserhalb des Quellbaums.
    """
    from moneten.config import Settings

    with caplog.at_level(logging.WARNING, logger="moneten.config"):
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'moneten.db').as_posix()}",
            secret_key="probe-schluessel-lang-genug-fuer-den-test",
            initial_pin="424242",
        )
    meldungen = " ".join(r.getMessage() for r in caplog.records)
    assert "ausserhalb des Projekts" not in meldungen, f"unnötige Warnung: {meldungen!r}"
