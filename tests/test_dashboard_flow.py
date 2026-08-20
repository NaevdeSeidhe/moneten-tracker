"""Der Geldfluss auf der Uebersicht: Zeitraum, Beschriftung, Leerzustand.

Der Fehler dahinter: Das Sankey-Diagramm wurde aus den Buchungen des LAUFENDEN
Monats gebaut, und die Karte hing als Ganzes an ``{% if flow %}``. Der Nutzer
importiert seine Bankdaten aber etwa einmal im Monat — es gibt keine Live-
Schnittstelle. Zwischen zwei Importen war der laufende Monat leer, die Karte
verschwand spurlos, und er hielt sie fuer geloescht.

Drei Zusagen werden hier geprueft:

1. leerer laufender Monat → juengster Monat MIT Buchungen (wie der
   Steuerjahr-Auszug auf ein Jahr mit Daten ausweicht);
2. der gezeigte Zeitraum steht am Diagramm;
3. gibt es nirgends Daten, sagt die Karte das — statt zu verschwinden — und
   bietet den Weg zum Import an.

Alle Daten sind erfunden. Die Helfer-Tests laufen auf einer EIGENEN Datenbank:
``_monate_mit_buchungen`` sieht alle Buchungen des Rueckblicks, auf der
geteilten Test-DB haenge das Ergebnis sonst davon ab, welches Testmodul vorher
lief.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.dates import add_months
from moneten.db.models import Account, AccountType, Category, ManagementType, Transaction
from moneten.db.session import SessionLocal
from moneten.routers.dashboard import _monate_mit_buchungen
from moneten.services.sankey import build_flow, entzerre_labels
from moneten.templating import MONATE

HEUTE = date(2026, 8, 6)  # der Tag, an dem der Nutzer die leere Karte meldete


@pytest.fixture
def eigene_db():
    """Leeres Schema in einer eigenen Datei — nur fuer diesen einen Test."""
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from moneten.db.models import Base

    with tempfile.TemporaryDirectory() as ordner:
        motor = create_engine(f"sqlite:///{Path(ordner) / 'flow.db'}")
        Base.metadata.create_all(motor)
        with sessionmaker(bind=motor)() as db:
            yield db
        motor.dispose()


def _konto(db) -> Account:
    konto = Account(name="ZZZ Testkonto", type=AccountType.BANK, currency="CHF",
                    is_active=True, opening_balance=Decimal("0"),
                    current_balance=Decimal("0"), sort_order=1)
    db.add(konto)
    db.flush()
    return konto


def _buchung(db, konto: Account, tag: date, betrag: str,
             art: ManagementType | None = None) -> None:
    db.add(Transaction(account_id=konto.id, date=tag, amount=Decimal(betrag),
                       description="ZZZ Buchung", management_type=art))


# ---------------------------------------------------------------------------
# 1. Welcher Monat wird gezeigt
# ---------------------------------------------------------------------------


def test_weicht_auf_juengsten_monat_mit_buchungen_aus(eigene_db) -> None:
    """Leerer August, Buchungen im Juni und Juli → Juli zuerst, dann Juni."""
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2026, 6, 14), "-120")
    _buchung(eigene_db, konto, date(2026, 7, 3), "-80")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == [date(2026, 7, 1), date(2026, 6, 1)]


def test_laufender_monat_hat_vorrang(eigene_db) -> None:
    """Sobald der laufende Monat Buchungen hat, gilt er — auch mit aelteren."""
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2026, 6, 14), "-120")
    _buchung(eigene_db, konto, HEUTE, "-9.80")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE)[0] == date(2026, 8, 1)


def test_kuenftige_buchungen_zaehlen_nicht(eigene_db) -> None:
    """Ein vorerfasster Dauerauftrag im Oktober darf den Zeitraum nicht setzen.

    Sonst zeigte die Karte einen Monat, der noch gar nicht stattgefunden hat.
    """
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2026, 6, 14), "-120")
    _buchung(eigene_db, konto, date(2026, 10, 1), "-1450")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == [date(2026, 6, 1)]


def test_kuenftige_buchung_im_laufenden_monat_zaehlt_auch_nicht(eigene_db) -> None:
    """Der Dauerauftrag auf den 16., waehrend heute der 6. ist.

    Die frueherer Fassung schnitt erst am Monatsende ab und liess damit genau
    diesen Fall durch: die Karte sprang auf den laufenden Monat, zeigte dort den
    Abgang fuer Geld, das noch niemand ausgegeben hat, und der Monat mit den
    echten Zahlen blieb verborgen — ohne Hinweis, weil ja nicht ausgewichen
    wurde. Nur ein spaeterer MONAT war geprueft, nicht ein spaeterer Tag.
    """
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2026, 6, 14), "-120")
    _buchung(eigene_db, konto, date(2026, 8, 16), "-1780")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == [date(2026, 6, 1)]


def test_heutige_buchung_zaehlt_noch(eigene_db) -> None:
    """Die Gegenprobe zur Zukunftsgrenze: heute ist nicht morgen.

    Ohne sie liesse sich die Grenze auch auf ``< heute`` schieben, und der
    Import von heute Morgen waere bis Mitternacht unsichtbar.
    """
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, HEUTE, "-42")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == [date(2026, 8, 1)]


def test_reiner_umbuchungsmonat_wird_uebersprungen(eigene_db) -> None:
    """Ein Monat mit nur Umbuchungen ergaebe ein leeres Diagramm.

    Das Diagramm laesst Transfers aus (weder Einnahme noch Ausgabe). Zaehlte der
    Zeitraum sie mit, stuende „Geldfluss · Juli 2026" ueber einer leeren Karte.
    """
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2026, 6, 14), "-120")
    _buchung(eigene_db, konto, date(2026, 7, 9), "-500", ManagementType.TRANSFER)
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == [date(2026, 6, 1)]


def test_ohne_jede_buchung_kein_monat(eigene_db) -> None:
    _konto(eigene_db)
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == []


def test_alte_buchungen_jenseits_des_rueckblicks_zaehlen_nicht(eigene_db) -> None:
    """Zwei Jahre alte Daten sind kein „aktueller" Geldfluss mehr.

    Ohne Grenze liefe die Rueckwaertssuche im Extremfall durch den ganzen
    Bestand, und die Karte behauptete einen Zeitraum, der niemanden mehr
    interessiert.
    """
    konto = _konto(eigene_db)
    _buchung(eigene_db, konto, date(2024, 6, 14), "-120")
    eigene_db.commit()

    assert _monate_mit_buchungen(eigene_db, HEUTE) == []


# ---------------------------------------------------------------------------
# 2./3. Was auf der Seite steht
# ---------------------------------------------------------------------------


@contextmanager
def _alle_buchungen_verschoben(jahre: int):
    """Verschiebt ALLE Buchungen um ``jahre`` und setzt sie danach zurueck.

    Mit +3 liegt nichts mehr in der Vergangenheit — der Zustand „noch gar keine
    Daten", ohne welche zu loeschen. Mit -3 ist der laufende Monat leer, aeltere
    Monate haben aber Buchungen.

    Verschoben wird in Tagen und nicht ueber ``date.replace(year=…)``: das faellt
    beim 29. Februar auf die Nase.
    """
    versatz = timedelta(days=365 * jahre)
    with SessionLocal() as db:
        alle = list(db.scalars(select(Transaction)))
        original = {t.id: t.date for t in alle}
        for t in alle:
            t.date = t.date + versatz
        db.commit()
    try:
        yield
    finally:
        with SessionLocal() as db:
            for tx_id, datum in original.items():
                t = db.get(Transaction, tx_id)
                if t is not None:
                    t.date = datum
            db.commit()


@contextmanager
def _buchungen(*posten: tuple[date, str], kategorie_id: int | None = None):
    """Legt Buchungen in der Test-DB an und raeumt sie danach weg.

    Ohne ``kategorie_id`` haben sie KEINE Kategorie — der Geldfluss buendelt sie
    dann unter „Uebrige Einnahmen" bzw. „Uebrige Ausgaben", und der Test haengt
    nicht am Kategorienbestand. Mit Kategorie werden sie gegeneinander
    verrechnet; das braucht der Test, der einen Monat sucht, der sich aufhebt.
    """
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        txs = [Transaction(account_id=konto.id, date=tag, amount=Decimal(betrag),
                           description="ZZZ Geldfluss-Test", category_id=kategorie_id)
               for tag, betrag in posten]
        db.add_all(txs)
        db.commit()
        ids = [t.id for t in txs]
    try:
        yield
    finally:
        with SessionLocal() as db:
            for tx_id in ids:
                t = db.get(Transaction, tx_id)
                if t is not None:
                    db.delete(t)
            db.commit()


def _kopf(seite: str) -> str:
    """Die Kopfzeile der Geldfluss-Karte — ohne sie ist nichts nachweisbar."""
    marke = '<div class="label-cap card-head">Geldfluss'
    assert marke in seite, "Die Geldfluss-Karte fehlt auf der Uebersicht"
    ab = seite.index(marke)
    return seite[ab:seite.index("</div>", ab)]


def test_zeitraum_steht_am_diagramm(logged_in_client: TestClient) -> None:
    """Ein Diagramm ohne Zeitraum-Angabe waere schlimmer als gar keines."""
    heute = date.today()
    with _buchungen((heute, "4200"), (heute, "-1300")):
        seite = logged_in_client.get("/").text
        kopf = _kopf(seite)
    assert f"{MONATE[heute.month - 1]} {heute.year}" in kopf, (
        f"Der gezeigte Monat steht nicht am Diagramm: {kopf!r}"
    )
    assert "ist noch ohne Buchungen" not in seite, (
        "Der Hinweis auf den leeren Monat steht da, obwohl der Monat Buchungen hat"
    )


def test_karte_bleibt_im_leeren_monat_stehen(logged_in_client: TestClient) -> None:
    """Der gemeldete Fall: seit dem letzten Import ist ein Monatswechsel her."""
    heute = date.today()
    vorher = add_months(heute.replace(day=1), -2)  # Monat des letzten Imports
    with _alle_buchungen_verschoben(-3), _buchungen(
        (vorher.replace(day=4), "4200"), (vorher.replace(day=9), "-1300")
    ):
        seite = logged_in_client.get("/").text
        kopf = _kopf(seite)

    assert "flow-svg" in seite, (
        "Die Karte zeigt kein Diagramm, obwohl es aeltere Buchungen gibt"
    )
    assert f"{MONATE[vorher.month - 1]} {vorher.year}" in kopf, (
        f"Der Geldfluss weicht nicht auf den Monat mit Daten aus: {kopf!r}"
    )
    assert f"{MONATE[heute.month - 1]} {heute.year} ist noch ohne Buchungen" in seite, (
        "Der abweichende Zeitraum wird nicht erklaert"
    )


def test_karte_sagt_warum_sie_leer_ist(logged_in_client: TestClient) -> None:
    """Nirgends Daten: sagen warum und zum Import fuehren, statt verschwinden.

    Eine Karte, die sich lautlos ausblendet, ist von einer entfernten nicht zu
    unterscheiden — genau das hat der Nutzer angenommen.
    """
    with _alle_buchungen_verschoben(3):
        seite = logged_in_client.get("/").text
        _kopf(seite)  # Karte da?
        assert "flow-svg" not in seite, "Vorbedingung: es gibt nichts zu zeichnen"
        assert "Noch keine Buchungen erfasst." in seite, (
            "Die leere Karte nennt keinen Grund"
        )
        # NICHT auf 'href="/import"' pruefen: das steht dreimal in der Navigation
        # und blieb gruen, als der Knopf aus der Karte geloescht wurde
        # (nachgemessen). Die eigene Klasse gibt es nur an diesem Knopf.
        assert "flow-import" in seite, "Aus der leeren Karte fuehrt kein Weg zum Import"


def test_ausweichmonat_der_sich_aufhebt_wird_uebersprungen(
        logged_in_client: TestClient) -> None:
    """Kopfzeile und Inhalt muessen denselben Monat meinen.

    „Monat hat Buchungen" und „Diagramm hat etwas zu zeigen" sind nicht dasselbe:
    ein Notebook-Kauf und dessen Gutschrift im selben Monat heben sich auf.
    Gemessen stand dann „Geldfluss · Juni 2026" ueber einer leeren Karte — mit
    einem fremden Monat im Kopf und ohne jede Erklaerung.
    """
    heute = date.today()
    juengster = add_months(heute.replace(day=1), -1)   # hebt sich auf
    aelterer = add_months(heute.replace(day=1), -2)    # hat echten Fluss
    with SessionLocal() as db:
        kat_id = db.scalars(select(Category).where(Category.parent_id.is_(None))).first().id
    with _alle_buchungen_verschoben(-3), _buchungen(
        (juengster.replace(day=5), "899"), (juengster.replace(day=6), "-899"),
        kategorie_id=kat_id,
    ), _buchungen(
        (aelterer.replace(day=4), "4200"), (aelterer.replace(day=9), "-1300"),
    ):
        seite = logged_in_client.get("/").text
        kopf = _kopf(seite)

    assert "flow-svg" in seite, "Der Ausweichmonat wurde nicht weitergesucht"
    assert f"{MONATE[aelterer.month - 1]} {aelterer.year}" in kopf, (
        f"Im Kopf steht ein Monat, zu dem das Diagramm nichts zeigt: {kopf!r}"
    )


def test_hinweis_steht_auch_ohne_diagramm(logged_in_client: TestClient) -> None:
    """Der Hinweis sass in ``{% if flow %}`` und fehlte genau dann, wenn er zaehlt.

    Steht im Kopf ein anderer Monat als im Rest der Seite, muss der Grund
    dabeistehen — erst recht, wenn ausserdem kein Diagramm da ist.
    """
    heute = date.today()
    vorher = add_months(heute.replace(day=1), -2)
    with _alle_buchungen_verschoben(-3), _buchungen(
        (vorher.replace(day=4), "4200"), (vorher.replace(day=9), "-1300")
    ):
        seite = logged_in_client.get("/").text
    hinweis = f"{MONATE[heute.month - 1]} {heute.year} ist noch ohne Buchungen"
    assert hinweis in seite
    # Der Hinweis muss VOR dem Diagramm stehen, sonst erklaert er nichts mehr.
    assert seite.index(hinweis) < seite.index("flow-svg")


# ---------------------------------------------------------------------------
# 4. Beschriftung: nichts liegt uebereinander
# ---------------------------------------------------------------------------


def test_entzerrer_haelt_den_mindestabstand() -> None:
    """Drei Kleinbetraege liegen im Diagramm praktisch aufeinander."""
    y = entzerre_labels([100.0, 101.0, 102.0], 400.0, abstand=26.0)
    assert all(b - a >= 26.0 - 1e-9 for a, b in zip(y, y[1:], strict=False)), y


def test_entzerrer_laesst_weite_labels_in_ruhe() -> None:
    """Was schon Platz hat, darf sich nicht verschieben — sonst luege der Text
    ueber die Lage seines Balkens, ohne dass es noetig waere."""
    assert entzerre_labels([50.0, 120.0, 260.0], 400.0, abstand=26.0) == [50.0, 120.0, 260.0]


def test_entzerrer_schiebt_nichts_aus_dem_bild() -> None:
    """Der erste Durchgang drueckt nach unten; ohne Rueckweg faellt das letzte
    Label aus dem viewBox."""
    y = entzerre_labels([380.0, 385.0, 390.0], 400.0, abstand=26.0, von=22.0, bis=378.0)
    assert y[-1] <= 378.0, y
    assert y[0] >= 22.0, y
    assert all(b - a >= 26.0 - 1e-9 for a, b in zip(y, y[1:], strict=False)), y


def test_entzerrer_behaelt_die_reihenfolge() -> None:
    """Ein Label, das an seinem Nachbarn vorbeirutscht, zeigt auf den falschen
    Balken — schlimmer als eine Ueberlappung."""
    y = entzerre_labels([200.0, 201.0, 202.0, 203.0], 400.0, abstand=26.0)
    assert y == sorted(y), y


def test_balken_bleibt_auf_seiner_mitte() -> None:
    """Entzerrt wird der TEXT. Verschoebe sich der Balken mit, wuerde das
    Diagramm die Betraege falsch darstellen."""
    flow = build_flow(
        [("Lohn", Decimal("5000"))],
        [("Miete", Decimal("1780")), ("Handy", Decimal("39.90")),
         ("Musterstadt", Decimal("42.10")), ("Rest", Decimal("4617"))],
    )
    for n in flow["right"]:
        assert n["mid"] == n["y0"] + n["h"] / 2, n["label"]


def test_beschriftungen_ueberlappen_nicht(logged_in_client: TestClient) -> None:
    """Der Fall aus der Messung: winzige Kategorien neben grossen.

    Geprueft wird am gerenderten Modell und nicht im Browser, damit die Zusage
    bei jedem Testlauf gilt — im Browser nachgemessen wurde sie zusaetzlich.
    """
    flow = build_flow(
        [("Lohn", Decimal("5000")), ("Nebenverdienst", Decimal("400"))],
        [("Wohnen", Decimal("2066.40")), ("Versicherungen", Decimal("2752.55")),
         ("Lebenshaltung", Decimal("620.15")), ("Gesundheit", Decimal("88.90")),
         ("Mobilität", Decimal("142.30")), ("Kommunikation", Decimal("39.90"))],
    )
    for spalte in ("left", "right"):
        ys = [n["label_y"] for n in flow[spalte]]
        eng = [(a, b) for a, b in zip(ys, ys[1:], strict=False) if b - a < 26.0 - 1e-9]
        assert not eng, f"{spalte}: Beschriftungen liegen uebereinander bei {eng}"


def test_unkategorisiertes_wird_nicht_gegeneinander_verrechnet(
        logged_in_client: TestClient) -> None:
    """Im Resttopf liegen Lohn und Miete nebeneinander.

    Eingang und Ausgang ohne Kategorie ergaben dort EINE Zeile ueber die
    Differenz — eine Zahl, die im Bestand nirgends steht. Innerhalb einer
    Kategorie ist die Verrechnung richtig (eine Rueckerstattung mindert den
    Aufwand), zwischen fremden Buchungen ist sie falsch.
    """
    heute = date.today()
    with _buchungen((heute, "5000"), (heute, "-4000")):
        seite = logged_in_client.get("/").text
    assert "Übrige Einnahmen" in seite and "Übrige Ausgaben" in seite, (
        "Einnahmen und Ausgaben ohne Kategorie wurden gegeneinander verrechnet"
    )


# ---------------------------------------------------------------------------
# 5. „Groesste Ausgaben" zieht mit
# ---------------------------------------------------------------------------
#
# Der Ausweichmonat gehoert nicht dem Geldfluss allein. Die Treemap darueber
# rechnete weiter den laufenden Monat und meldete „noch keine Ausgaben erfasst",
# waehrend direkt darunter der Juni stand: zwei Karten, zwei Zeitraeume, keine
# Erklaerung. Zwei VERSCHIEDENE Ausweichmonate waeren noch schlimmer — darum
# pruefen diese Tests nicht nur, DASS ein Monat im Kopf steht, sondern dass es
# derselbe ist.


def _kopf_ausgaben(seite: str) -> str:
    """Die Kopfzeile der Karte „Groesste Ausgaben"."""
    marke = '<div class="label-cap card-head">Grösste Ausgaben'
    assert marke in seite, "Die Karte „Groesste Ausgaben“ fehlt auf der Uebersicht"
    ab = seite.index(marke)
    return seite[ab:seite.index("</div>", ab)]


@contextmanager
def _buchungen_je_kategorie(tag: date, posten: list[tuple[int, str]]):
    """Wie ``_buchungen``, aber je Buchung eine EIGENE Kategorie.

    Gebraucht, wo die ANZAHL der Kategorien den Ausschlag gibt: die Treemap
    zeigt Blatt-Kategorien, und ihre Obergrenze laesst sich nur pruefen, wenn
    mehr Kategorien da sind, als sie zeigen darf.
    """
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        txs = [Transaction(account_id=konto.id, date=tag, amount=Decimal(betrag),
                           description="ZZZ Kachel-Test", category_id=kat_id)
               for kat_id, betrag in posten]
        db.add_all(txs)
        db.commit()
        ids = [t.id for t in txs]
    try:
        yield
    finally:
        with SessionLocal() as db:
            for tx_id in ids:
                t = db.get(Transaction, tx_id)
                if t is not None:
                    db.delete(t)
            db.commit()


def _blatt_kategorien(anzahl: int) -> list[int]:
    """Irgendwelche ``anzahl`` Unterkategorien — welche, ist gleichgueltig."""
    with SessionLocal() as db:
        ids = [c.id for c in db.scalars(
            select(Category).where(Category.parent_id.is_not(None)).order_by(Category.id)
        ).all()[:anzahl]]
    assert len(ids) == anzahl, "Testaufbau erwartet genug Unterkategorien"
    return ids


def _geschwister_kategorien() -> tuple[int, str, int]:
    """Zwei Unterkategorien DERSELBEN Gruppe: (Ausgabe-Id, deren Name, Gutschrift-Id).

    Welche Gruppe es ist, spielt keine Rolle — es zaehlt allein, dass beide
    Buchungen zur selben TOP-Kategorie gehoeren und sich dort aufheben.
    """
    with SessionLocal() as db:
        for eltern in db.scalars(select(Category).where(Category.parent_id.is_(None))).all():
            kinder = db.scalars(select(Category).where(Category.parent_id == eltern.id)).all()
            if len(kinder) >= 2:
                return kinder[0].id, kinder[0].name, kinder[1].id
    raise AssertionError("Testaufbau erwartet eine Gruppe mit zwei Unterkategorien")


def _top_kategorie() -> tuple[int, str]:
    """Irgendeine Top-Kategorie — ohne sie taucht eine Ausgabe nicht als Kachel
    auf: die Treemap zeigt Kategorien, Unkategorisiertes hat keine."""
    with SessionLocal() as db:
        kat = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
        return kat.id, kat.name


def test_ausgaben_folgen_dem_ausweichmonat_des_geldflusses(
        logged_in_client: TestClient) -> None:
    """Beide Karten nennen denselben Monat, und die Kacheln stammen daraus.

    Die zweite Zusage ist die eigentliche: der Kopf allein liesse sich auch
    setzen, waehrend die Flaechen weiter den (leeren) laufenden Monat zeigen.
    """
    heute = date.today()
    vorher = add_months(heute.replace(day=1), -2)  # Monat des letzten Imports
    kat_id, _ = _top_kategorie()
    with _alle_buchungen_verschoben(-3), _buchungen(
        (vorher.replace(day=4), "4200")
    ), _buchungen(
        (vorher.replace(day=9), "-1300"), kategorie_id=kat_id
    ):
        seite = logged_in_client.get("/").text

    monat = f"{MONATE[vorher.month - 1]} {vorher.year}"
    assert monat in _kopf(seite), "Vorbedingung: der Geldfluss weicht aus"
    assert monat in _kopf_ausgaben(seite), (
        f"Die Ausgaben-Karte nennt einen anderen Zeitraum: {_kopf_ausgaben(seite)!r}"
    )
    assert "tm-tile" in seite, (
        "Die Ausgaben-Karte ist leer, obwohl der gezeigte Monat Ausgaben hat — "
        "sie rechnet noch den laufenden Monat"
    )


def test_ausgaben_nennen_den_zeitraum_auch_im_laufenden_monat(
        logged_in_client: TestClient) -> None:
    """Der Zeitraum steht IMMER im Kopf, nicht nur bei Abweichung.

    Sonst muss der Leser im Regelfall raten, welcher Monat gemeint ist — und
    merkt beim naechsten Ausweichen nicht, dass sich etwas geaendert hat.
    """
    heute = date.today()
    kat_id, _ = _top_kategorie()
    with _buchungen((heute, "4200")), _buchungen((heute, "-1300"), kategorie_id=kat_id):
        seite = logged_in_client.get("/").text

    monat = f"{MONATE[heute.month - 1]} {heute.year}"
    assert monat in _kopf_ausgaben(seite), (
        f"Im laufenden Monat fehlt der Zeitraum am Kopf: {_kopf_ausgaben(seite)!r}"
    )


def test_leere_ausgaben_karte_behauptet_keinen_zeitraum(
        logged_in_client: TestClient) -> None:
    """Der gemeldete Satz: „Noch keine Ausgaben in diesem Monat erfasst."

    Er meinte den laufenden Monat, waehrend der Kopf laengst einen anderen
    zeigte. Hat der Ausweichmonat nur unkategorisierte Ausgaben, bleibt die
    Flaeche leer — der Satz darf dann keinen Monat behaupten, den Kopf sagt es.
    """
    heute = date.today()
    vorher = add_months(heute.replace(day=1), -2)
    with _alle_buchungen_verschoben(-3), _buchungen(
        (vorher.replace(day=4), "4200"), (vorher.replace(day=9), "-1300")
    ):
        seite = logged_in_client.get("/").text

    monat = f"{MONATE[vorher.month - 1]} {vorher.year}"
    assert "tm-tile" not in seite, "Vorbedingung: es gibt keine Kachel zu zeigen"
    assert monat in _kopf_ausgaben(seite), "Der leeren Karte fehlt ihr Zeitraum"
    assert "Keine Ausgaben erfasst." in seite, "Die leere Karte sagt nicht, dass sie leer ist"
    assert "Ausgaben in diesem Monat" not in seite, (
        "Der Satz behauptet weiterhin den laufenden Monat"
    )


def test_ohne_jede_buchung_nennt_auch_die_ausgaben_karte_keinen_monat(
        logged_in_client: TestClient) -> None:
    """Derselbe Fehler wie vorher, nur umgekehrt — und der aeltere Zustand.

    Gibt es nirgends eine Buchung, nennt der Geldfluss keinen Monat: es gibt
    keinen. Die Ausgaben-Karte schrieb den laufenden trotzdem in ihren Kopf und
    behauptete damit einen Zeitraum, zu dem nirgends eine Zahl steht — direkt
    unter einer Karte, die aus Prinzip keinen nennt. Zwei Karten, die als Paar
    gelesen werden sollen, mit zwei verschiedenen Zeitraum-Vertraegen.

    Auf der geteilten Test-DB ist der Zustand nicht herstellbar (dort liegen
    immer Buchungen) — daher der Griff zum Verschieben.
    """
    heute = date.today()
    with _alle_buchungen_verschoben(3):
        seite = logged_in_client.get("/").text
        kopf_flow = _kopf(seite)
        kopf_ausgaben = _kopf_ausgaben(seite)

    monat = f"{MONATE[heute.month - 1]} {heute.year}"
    assert "Noch keine Buchungen erfasst." in seite, "Vorbedingung: es gibt keine Daten"
    assert "·" not in kopf_flow, f"Vorbedingung: der Geldfluss nennt keinen Monat: {kopf_flow!r}"
    assert monat not in kopf_ausgaben, (
        f"Die Ausgaben-Karte behauptet einen Monat ohne jede Zahl: {kopf_ausgaben!r}"
    )
    # Auch kein Trennpunkt: ohne die Bedingung in der Vorlage steht dort
    # „Grösste Ausgaben · None" — kein Monat, aber ebenso wenig der Kopf, den
    # die Karte darüber trägt.
    assert "·" not in kopf_ausgaben, (
        f"Der Kopf trägt einen Trennpunkt ohne Zeitraum: {kopf_ausgaben!r}"
    )


def test_kacheln_stehen_auch_ohne_geldfluss_und_nennen_ihren_monat(
        logged_in_client: TestClient) -> None:
    """Kein Diagramm, trotzdem Kacheln — und die Karte sagt, aus welchem Monat.

    „Geldfluss hat etwas zu zeigen" und „es gibt Ausgaben" sind nicht dasselbe:
    der Geldfluss rechnet netto je TOP-Kategorie, die Kacheln je Blatt. Ein Kauf
    in der einen und dessen Gutschrift in einer anderen UNTERkategorie derselben
    Gruppe heben sich fuer das Diagramm auf — die Ausgabe steht aber sehr wohl
    noch da. Kein Kandidatenmonat traegt dann, und was die Karte zeigt,
    entscheidet allein die Rueckfall-Vorbelegung auf den laufenden Monat.
    """
    heute = date.today()
    aus_id, aus_name, gut_id = _geschwister_kategorien()
    with _alle_buchungen_verschoben(3), _buchungen_je_kategorie(
        heute, [(aus_id, "-5000"), (gut_id, "5000")]
    ):
        seite = logged_in_client.get("/").text

    assert "flow-svg" not in seite, "Vorbedingung: der Geldfluss hebt sich auf"
    assert "gegenseitig auf" in seite, "Vorbedingung: die Karte nennt genau diesen Grund"
    assert "tm-tile" in seite, (
        "Die Ausgaben-Karte ist leer, obwohl der laufende Monat eine Ausgabe hat"
    )
    assert aus_name in seite, "Die Kachel traegt nicht die Kategorie der Ausgabe"
    monat = f"{MONATE[heute.month - 1]} {heute.year}"
    assert monat in _kopf_ausgaben(seite), (
        f"Die Kacheln stehen ohne Zeitraum da: {_kopf_ausgaben(seite)!r}"
    )


def test_karte_zeigt_hoechstens_sieben_kategorien(logged_in_client: TestClient) -> None:
    """Wie viele Kacheln die Karte zeigt, ist eine Entscheidung — und ungeprueft.

    Aus der Geometrie folgt sie nicht: nachgemessen haengt es an der Verteilung
    der Betraege und nicht an ihrer Anzahl, ob die kleinste Kachel ihre
    Beschriftung noch traegt. Gerade darum muss die gewaehlte Zahl hier stehen.
    Ohne diesen Test liess sie sich auf 1 oder 3 setzen, und die Karte zeigte
    still weniger, als sie soll.
    """
    heute = date.today()
    kats = _blatt_kategorien(9)
    betraege = ["-900", "-800", "-700", "-600", "-500", "-400", "-300", "-200", "-100"]
    with _alle_buchungen_verschoben(3), _buchungen_je_kategorie(
        heute, list(zip(kats, betraege, strict=True))
    ):
        seite = logged_in_client.get("/").text

    assert seite.count("tm-tile") == 7, (
        f"Die Karte zeigt {seite.count('tm-tile')} Kacheln statt sieben"
    )
