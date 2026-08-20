"""Die Karte „Was kommt" muss unabhängig vom Rest der Übersicht erscheinen.

Regressionstest zu einem Fehler, den erst die Deploy-Prüfung fand: Beim Einbauen
wurde die Karte vor die Geldfluss-Karte gesetzt — aber die steckt in einem
``{% if flow %}``, und das öffnete zwei Zeilen weiter oben. Damit hingen die
Fristen am Sankey-Diagramm, das ``None`` ist, sobald der laufende Monat weder
Einnahmen noch Ausgaben hat.

Folge: In jedem noch buchungsleeren Monat — also typischerweise gleich nach dem
Monatswechsel, bis der Bankauszug importiert ist — verschwanden „Krankenkasse
kündigen bis 30.11." und „Säule 3a bis 31.12." lautlos. Genau dann, wenn man am
ehesten hinschaut.

Es gab Tests für den Service, aber keinen fürs Rendern. Der hier prüft die
Kombination, die niemand von Hand ausprobiert: Fristen vorhanden, Monat leer.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.dates import add_months
from moneten.db.models import Account, Transaction
from moneten.db.session import SessionLocal
from moneten.templating import MONATE


@contextmanager
def _ohne_buchungen_im_monat():
    """Verschiebt alle Buchungen des laufenden Monats vorübergehend ins Vorjahr.

    So entsteht der Zustand „Monat noch leer", ohne Daten zu verlieren: die
    Buchungen werden am Ende wieder auf ihr echtes Datum zurückgesetzt.
    """
    heute = date.today()
    monatsanfang = heute.replace(day=1)
    with SessionLocal() as db:
        betroffen = list(db.scalars(
            select(Transaction).where(Transaction.date >= monatsanfang)
        ))
        original = {t.id: t.date for t in betroffen}
        for t in betroffen:
            t.date = t.date.replace(year=t.date.year - 3)
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
def _mit_anstehendem_posten():
    """Legt einen erkannten Jahresposten an, der sicher im Horizont liegt.

    Zwei Zahlungen im selben Monat zweier Vorjahre — genau das Muster, das
    ``jahresposten()`` erkennt. Der Zielmonat wird als „in zwei Monaten"
    berechnet, damit der Test an jedem Kalendertag dasselbe prueft; die
    hinterlegten Fristen (30.11., 31.12., 31.3.) liegen die meiste Zeit des
    Jahres ausserhalb des Drei-Monats-Fensters und taugen nicht als Grundlage.
    """
    heute = date.today()
    ziel = add_months(heute.replace(day=1), 2)
    text = "ZZZanstehend" + "".join(
            chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:5]
        )
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        txs = [
            Transaction(account_id=konto.id, date=date(heute.year - j, ziel.month, 14),
                        amount=Decimal("-480"), description=text)
            for j in (1, 2)
        ]
        db.add_all(txs)
        db.commit()
        ids = [t.id for t in txs]
    try:
        yield text
    finally:
        with SessionLocal() as db:
            for tx_id in ids:
                t = db.get(Transaction, tx_id)
                if t is not None:
                    db.delete(t)
            db.commit()


def _hat_was_kommt(client: TestClient) -> bool:
    return 'class="label-cap card-head">Was kommt<' in client.get("/").text


def _hat_geldfluss(client: TestClient) -> bool:
    return "flow-card" in client.get("/").text


def test_karte_erscheint_auch_ohne_buchungen_im_monat(logged_in_client: TestClient) -> None:
    """Der eigentliche Fehler: leerer Monat, aber es steht trotzdem etwas an."""
    with _mit_anstehendem_posten() as text, _ohne_buchungen_im_monat():
        seite = logged_in_client.get("/").text
        # Vorbedingung: der laufende Monat ist wirklich leer. Ablesbar am
        # Geldfluss — der weicht dann auf einen aelteren Monat aus und traegt
        # dessen Namen im Kopf (seit 08/2026; vorher verschwand die Karte).
        heute = date.today()
        assert f"Geldfluss · {MONATE[heute.month - 1]} {heute.year}" not in seite, (
            "Vorbedingung des Tests: der laufende Monat darf keine Buchungen haben"
        )
        assert 'class="label-cap card-head">Was kommt<' in seite, (
            "Die Karte haengt wieder am Geldfluss-Diagramm und verschwindet "
            "in jedem buchungsleeren Monat"
        )
        assert text in seite, "Der anstehende Posten fehlt in der Karte"


def test_karte_liegt_nicht_im_geldfluss_block() -> None:
    """Strukturprüfung am Template — unabhängig von Daten.

    Der Datentest oben kann nur greifen, wenn im Testbestand überhaupt Fristen
    anstehen. Diese Prüfung gilt immer.
    """
    from pathlib import Path

    tpl = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
           / "dashboard.html").read_text(encoding="utf-8")
    i_kommt = tpl.index("{% if kommt.posten %}")
    i_flow = tpl.index("{% if flow %}")
    assert i_kommt < i_flow, (
        'Die Karte steht im Template hinter {% if flow %} — damit haengt sie am '
        'Geldfluss-Diagramm, statt eigenstaendig zu erscheinen'
    )


def test_veraltet_warnung_haengt_nicht_am_geldfluss(logged_in_client: TestClient) -> None:
    """Ab 2027 warnt die App, dass die Fristen-Konstanten überholt sind.

    Diese Warnung sitzt IN der Karte. Hängt die Karte an einer Bedingung, greift
    auch die Warnung nur zufällig — und eine Warnung, die man nur manchmal sieht,
    ist keine.
    """
    from pathlib import Path

    tpl = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
           / "dashboard.html").read_text(encoding="utf-8")
    i_veraltet = tpl.index("{% if kommt.veraltet %}")
    i_flow = tpl.index("{% if flow %}")
    assert i_veraltet < i_flow


def test_uebersicht_bleibt_ohne_jede_buchung_heil(logged_in_client: TestClient) -> None:
    """Leere Datenbestände sind der häufigste Absturzgrund nach einem Deploy."""
    with _ohne_buchungen_im_monat():
        resp = logged_in_client.get("/")
        assert resp.status_code == 200
        assert "Übersicht" in resp.text


def test_jahresposten_erscheint_auf_der_seite(logged_in_client: TestClient) -> None:
    """Ende-zu-Ende: erkannter Jahresposten muss auch gerendert werden.

    Bisher prueften nur die Service-Tests. Dass ihr Ergebnis die Seite erreicht,
    hat niemand nachgesehen — genau dort lag der Fehler.
    """
    with _mit_anstehendem_posten() as text:
        seite = logged_in_client.get("/").text
        assert text in seite, "Der erkannte Jahresposten steht nicht auf der Uebersicht"
        assert re.search(r'class="kommt-zeile"', seite)
