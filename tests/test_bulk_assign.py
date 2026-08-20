"""Massen-Zuweisung auf der Buchungen-Seite: alle Treffer eines Filters auf einmal.

Die Tests hier sichern die vier Stellen, an denen diese Funktion teuer schiefgeht:

* Sie trifft nur die **erste Seite**. Die Liste lädt Monatskarten fensterweise
  nach (``before=``); wer über die gerenderten Buchungen iteriert, ordnet 18 von
  195 zu und merkt es nicht.
* Sie trifft etwas **ausserhalb** des Filters. Der teuerste Fehler überhaupt —
  eine falsch kategorisierte Buchung fällt Monate später auf, wenn überhaupt.
* **Vorschau und Wirkung laufen auseinander.** Steht „195" über dem Knopf und
  ändert er 18, ist die Vorschau schlimmer als keine: sie erzeugt Vertrauen.
* **Rückgängig macht neuen Schaden.** Ein pauschales „alles wieder auf offen"
  vernichtet die Kategorien, die vor der Aktion schon dastanden.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Category, ManagementType, Transaction
from moneten.db.session import SessionLocal

SRC = Path(__file__).resolve().parents[1] / "src" / "moneten"

_zaehler = {"n": 0}


def _konto_id() -> int:
    """Frisches, isoliertes Konto — die Test-DB wird von vielen Modulen geteilt."""
    _zaehler["n"] += 1
    with SessionLocal() as db:
        acc = Account(name=f"Bulk-Test {_zaehler['n']}", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=900)
        db.add(acc)
        db.commit()
        return acc.id


def _kategorie(name: str) -> Category:
    with SessionLocal() as db:
        cat = db.scalar(select(Category).where(Category.name == name))
        assert cat is not None, f"Seed-Kategorie „{name}“ fehlt"
        db.expunge(cat)
        return cat


def _monate_zurueck(n: int) -> date:
    """Datum n Monate vor heute, immer am 15. — die Monatskarten der Liste sind
    das Fenster (``MONTH_WINDOW``), nicht ein Zeitraum in Tagen."""
    heute = date.today()
    monat = heute.month - n
    jahr = heute.year
    while monat < 1:
        monat += 12
        jahr -= 1
    return date(jahr, monat, 15)


def _buchung(
    konto_id: int, beschreibung: str, *, monate_zurueck: int = 0, betrag: str = "-20.00",
    kategorie_id: int | None = None, art: ManagementType | None = None,
    aufgeteilt: bool = False,
) -> int:
    with SessionLocal() as db:
        tx = Transaction(
            account_id=konto_id, category_id=kategorie_id,
            date=_monate_zurueck(monate_zurueck),
            amount=Decimal(betrag), description=beschreibung,
            management_type=art, is_split=aufgeteilt,
        )
        db.add(tx)
        db.commit()
        return tx.id


def _kategorie_von(tx_id: int) -> int | None:
    with SessionLocal() as db:
        return db.get(Transaction, tx_id).category_id


def _vorschau_zahl(client: TestClient, **filter_werte) -> int:
    """Die Zahl, die in der Leiste VOR dem Klick steht („Betrifft N Buchungen")."""
    resp = client.get("/transactions/bulk-bar", params=filter_werte,
                      headers={"HX-Request": "true"})
    assert resp.status_code == 200, resp.text
    treffer = re.search(r"Betrifft\s*<strong>(\d+)</strong>", resp.text)
    assert treffer, f"Keine Vorschau-Zahl in der Leiste:\n{resp.text[:600]}"
    return int(treffer.group(1))


def _leiste(client: TestClient, **filter_werte) -> str:
    """Die gerenderte Massen-Zuweisungs-Leiste zu einem Filter."""
    resp = client.get("/transactions/bulk-bar", params=filter_werte,
                      headers={"HX-Request": "true"})
    assert resp.status_code == 200, resp.text
    return resp.text


def _zuweisen_knopf(leiste: str) -> str:
    """Das öffnende <button>-Tag des Zuweisen-Knopfs (mit allen hx-Attributen)."""
    treffer = re.search(r"<button[^>]*tx-bulk-go[^>]*>", leiste, re.S)
    assert treffer, f"Kein Zuweisen-Knopf in der Leiste:\n{leiste[:800]}"
    return treffer.group(0)


def _knopf_werte(knopf: str) -> dict:
    """Die Werte, die der Knopf beim Klick mitschickt (``hx-vals``)."""
    treffer = re.search(r"hx-vals='([^']*)'", knopf)
    assert treffer, (
        "Der Zuweisen-Knopf trägt keine eingefrorenen Filterwerte (hx-vals). "
        f"Dann liest er die Felder erst im Moment des Klicks:\n{knopf}"
    )
    return json.loads(html.unescape(treffer.group(1)))


def _confirm_zahl(knopf: str) -> int:
    """Die Zahl, die in der Sicherheitsabfrage steht."""
    treffer = re.search(r'hx-confirm="(\d+) Buchung\(en\)', knopf)
    assert treffer, f"Keine Zahl in der Sicherheitsabfrage:\n{knopf}"
    return int(treffer.group(1))


def _zugeordnet_zahl(text: str) -> int:
    """Die Zahl aus der Rückmeldung („N Buchung(en) → Kategorie zugeordnet.")."""
    treffer = re.search(r"(\d+) Buchung\(en\) &#8594;|(\d+) Buchung\(en\) →", text)
    assert treffer, f"Keine Ergebnis-Zahl in der Antwort:\n{text[:600]}"
    return int(treffer.group(1) or treffer.group(2))


# ---------------------------------------------------------------------------


def test_zuweisung_trifft_auch_buchungen_jenseits_der_ersten_seite(
    logged_in_client: TestClient,
) -> None:
    """Verhindert: die Zuweisung fasst nur an, was die Liste gerade zeigt.

    Die Liste rendert nur die neuesten sechs Monate mit Treffern
    (``MONTH_WINDOW``); ältere holt „Ältere Monate laden" per ``before=`` nach.
    Würde die Zuweisung über die gerenderten Buchungen laufen statt über die
    Filterbedingung, blieben die beiden ältesten offen — bei 195 offenen
    Buchungen fällt so etwas erst auf, wenn die Jahresauswertung nicht stimmt.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Zuweisungsprobe Orellius"
    # Acht Monate mit je einer Buchung → zwei Monate liegen hinter dem Fenster.
    ids = [_buchung(konto, f"{marke} Monat {m}", monate_zurueck=m) for m in range(8)]

    # Beweis, dass der Fall überhaupt greift: die ältesten sind gar nicht gerendert.
    seite = logged_in_client.get("/transactions", params={"q": marke},
                                 headers={"HX-Request": "true"})
    assert f"{marke} Monat 0" in seite.text
    assert f"{marke} Monat 7" not in seite.text, \
        "Testaufbau kaputt: die älteste Buchung steht doch auf der ersten Seite"

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 200, resp.text
    offen = [t for t in ids if _kategorie_von(t) != cat.id]
    assert not offen, f"{len(offen)} Buchung(en) jenseits der ersten Seite blieben offen"


def test_zuweisung_trifft_nichts_ausserhalb_des_filters(
    logged_in_client: TestClient,
) -> None:
    """Verhindert den teuersten Fehler: eine Sammelaktion greift zu weit.

    Geprüft werden alle vier Filterachsen (Text, Konto, Art, Kategorie) sowie
    die beiden Sorten, die eine Massen-Zuweisung nie anfassen darf: aufgeteilte
    Buchungen (ihre Kategorien stehen in den Splits) und Umbuchungen (weder
    Ausgabe noch Einnahme).
    """
    konto = _konto_id()
    fremd_konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Abgrenzungsprobe Xylo"

    treffer = _buchung(konto, f"{marke} Laden", monate_zurueck=0)
    anderer_text = _buchung(konto, "Voellig anderer Haendler", monate_zurueck=0)
    anderes_konto = _buchung(fremd_konto, f"{marke} Laden", monate_zurueck=0)
    einnahme = _buchung(konto, f"{marke} Rueckerstattung", monate_zurueck=0, betrag="12.00")
    umbuchung = _buchung(konto, f"{marke} Umbuchung", monate_zurueck=0,
                         art=ManagementType.TRANSFER)
    aufgeteilt = _buchung(konto, f"{marke} Sammelbeleg", monate_zurueck=0, aufgeteilt=True)

    # Filter: Text + Konto + Art „Ausgabe" → nur die eine Buchung darf sich ändern.
    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "account_id": str(konto), "kind": "ausgabe",
        "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 200, resp.text

    assert _kategorie_von(treffer) == cat.id
    for tx_id, warum in (
        (anderer_text, "anderer Buchungstext"),
        (anderes_konto, "anderes Konto"),
        (einnahme, "Einnahme statt Ausgabe"),
        (umbuchung, "Umbuchung"),
        (aufgeteilt, "aufgeteilte Buchung"),
    ):
        assert _kategorie_von(tx_id) is None, f"Ausserhalb des Filters angefasst: {warum}"


def test_ohne_filter_wird_nichts_zugeordnet(logged_in_client: TestClient) -> None:
    """Verhindert die Vollkatastrophe: „alle Treffer" ohne Filter = der ganze Bestand.

    Ohne aktiven Filter zeigt die Oberfläche die Leiste gar nicht erst — aber ein
    abgeschickter Request darf sich darauf nicht verlassen.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    frei = _buchung(konto, "Ungefilterte Buchung Zeta", monate_zurueck=0)

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 400
    assert _kategorie_von(frei) is None

    # Suchtext aus lauter Leerzeichen ist KEIN Filter: die Bedingung würde zu
    # ``ilike("%%")`` und damit auf jede Buchung passen — die Schutzregel wäre
    # mit einem Leerschlag im Suchfeld ausgehebelt.
    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": "   ", "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 400
    assert _kategorie_von(frei) is None


def test_vorschau_zahl_entspricht_der_zahl_der_geaenderten(
    logged_in_client: TestClient,
) -> None:
    """Verhindert eine Vorschau, die mehr verspricht als der Knopf hält.

    Eine Vorschau, die nicht stimmt, ist schlimmer als keine: sie erzeugt genau
    das Vertrauen, das die Prüfung ersetzt. Geprüft wird beides — mit und ohne
    Überschreiben, denn nur der zweite Fall zählt die bereits zugeordneten mit.
    """
    konto = _konto_id()
    cat_alt = _kategorie("Kaffee")
    cat_neu = _kategorie("Hobby")
    marke = "Vorschauprobe Delta"

    offen = [_buchung(konto, f"{marke} {i}", monate_zurueck=i * 2) for i in range(5)]
    belegt = [_buchung(konto, f"{marke} alt {i}", monate_zurueck=1 + i * 3,
                       kategorie_id=cat_alt.id) for i in range(3)]

    # 1. Ohne Überschreiben: Vorschau spricht nur von den offenen.
    vorschau = _vorschau_zahl(logged_in_client, q=marke)
    assert vorschau == len(offen)
    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "assign_category_id": str(cat_neu.id),
    })
    assert _zugeordnet_zahl(resp.text) == vorschau
    assert all(_kategorie_von(t) == cat_neu.id for t in offen)
    assert all(_kategorie_von(t) == cat_alt.id for t in belegt), \
        "Bereits zugeordnete wurden ohne Freigabe überschrieben"

    # 2. Mit Überschreiben: Vorschau spricht von allen Treffern.
    vorschau_alle = _vorschau_zahl(logged_in_client, q=marke, overwrite="1")
    assert vorschau_alle == len(offen) + len(belegt)
    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "overwrite": "1", "assign_category_id": str(cat_neu.id),
    })
    assert _zugeordnet_zahl(resp.text) == vorschau_alle
    assert all(_kategorie_von(t) == cat_neu.id for t in offen + belegt)


def test_vorschau_zaehlt_auch_jenseits_der_ersten_seite(
    logged_in_client: TestClient,
) -> None:
    """Verhindert eine Vorschau, die nur die geladene Seite zählt.

    Die Zahl über dem Knopf muss über den ganzen Filter sprechen — sonst stünde
    dort „6", während der Knopf neun Buchungen ändert, und der Nutzer ginge
    blind auf Treffer los, die er nie gesehen hat.
    """
    konto = _konto_id()
    marke = "Fensterprobe Sigma"
    for m in range(9):
        _buchung(konto, f"{marke} Monat {m}", monate_zurueck=m)

    seite = logged_in_client.get("/transactions", params={"q": marke},
                                 headers={"HX-Request": "true"})
    assert f"{marke} Monat 8" not in seite.text, "Testaufbau kaputt: alles auf einer Seite"
    assert _vorschau_zahl(logged_in_client, q=marke) == 9


def test_rueckgaengig_stellt_gemischte_vorzustaende_wieder_her(
    logged_in_client: TestClient,
) -> None:
    """Verhindert, dass die Rücknahme selbst Schaden anrichtet.

    Ausgangslage gemischt: einige Buchungen offen, einige schon in zwei
    verschiedenen Kategorien. Ein pauschales „alle wieder auf offen" (so macht es
    das ältere Undo der Regel-Seite) würde beim Reparieren eines Fehlklicks die
    vorher korrekt gesetzten Kategorien vernichten — ein grösserer Schaden als
    der, der repariert werden sollte.
    """
    konto = _konto_id()
    cat_a = _kategorie("Kaffee")
    cat_b = _kategorie("Alkohol")
    cat_ziel = _kategorie("Hobby")
    marke = "Rueckgaengigprobe Omega"

    offen = [_buchung(konto, f"{marke} offen {i}", monate_zurueck=0 + i) for i in range(2)]
    in_a = [_buchung(konto, f"{marke} a {i}", monate_zurueck=3 + i, kategorie_id=cat_a.id)
            for i in range(2)]
    in_b = [_buchung(konto, f"{marke} b {i}", monate_zurueck=8 + i, kategorie_id=cat_b.id)
            for i in range(2)]

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "overwrite": "1", "assign_category_id": str(cat_ziel.id),
    })
    assert resp.status_code == 200, resp.text
    assert all(_kategorie_von(t) == cat_ziel.id for t in offen + in_a + in_b)

    # Der Vorzustand reist im HX-Trigger mit — genau das ruft der „Rückgängig"-
    # Knopf im Toast auf (siehe initUndoTriggers in app.js).
    trigger = json.loads(resp.headers["HX-Trigger"])["moneten:toast"]
    assert trigger["undo"]["url"] == "/transactions/assign-undo"
    zurueck = logged_in_client.post("/transactions/assign-undo", data=trigger["undo"]["values"])
    assert zurueck.status_code == 200, zurueck.text

    assert all(_kategorie_von(t) is None for t in offen), "offene Buchungen nicht wieder offen"
    assert all(_kategorie_von(t) == cat_a.id for t in in_a), "Vorkategorie A ging verloren"
    assert all(_kategorie_von(t) == cat_b.id for t in in_b), "Vorkategorie B ging verloren"


def test_rueckgaengig_nimmt_auch_die_verwaltungsart_zurueck(
    logged_in_client: TestClient,
) -> None:
    """Verhindert eine halbe Rücknahme: Kategorie zurück, Verwaltungsart nicht.

    Die Verwaltungsart folgt der Kategorie (Budget, Sankey und die
    Transfer-Logik hängen daran). Bliebe sie nach dem Rückgängig auf dem Wert
    der Ziel-Kategorie stehen, wäre die Buchung in der Liste wieder unauffällig,
    in den Auswertungen aber weiterhin falsch einsortiert.
    """
    konto = _konto_id()
    cat_ziel = _kategorie("Nettolohn")  # trägt management_type EINKOMMEN
    assert cat_ziel.management_type is not None, "Testaufbau: Kategorie ohne Verwaltungsart"
    marke = "Verwaltungsartprobe Kappa"
    tx = _buchung(konto, f"{marke} Zahlung", monate_zurueck=0)

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "assign_category_id": str(cat_ziel.id),
    })
    with SessionLocal() as db:
        assert db.get(Transaction, tx).management_type == cat_ziel.management_type

    trigger = json.loads(resp.headers["HX-Trigger"])["moneten:toast"]
    logged_in_client.post("/transactions/assign-undo", data=trigger["undo"]["values"])
    with SessionLocal() as db:
        nachher = db.get(Transaction, tx)
        assert nachher.category_id is None
        assert nachher.management_type is None


def test_leiste_erscheint_nur_bei_aktivem_filter(logged_in_client: TestClient) -> None:
    """Verhindert eine Leiste, die „alle Treffer" sagt und den ganzen Bestand meint.

    Ohne Filter ist die Trefferliste der komplette Bestand — dort ist ein
    Ein-Klick-Zuordnen keine Abkürzung, sondern eine Falle.
    """
    ohne = logged_in_client.get("/transactions", headers={"HX-Request": "true"})
    assert 'id="tx-bulk"' not in ohne.text

    mit = logged_in_client.get("/transactions", params={"q": "a"},
                               headers={"HX-Request": "true"})
    assert 'id="tx-bulk"' in mit.text


# ---------------------------------------------------------------------------
# Gegenprüfung: Befunde einer adversarischen Durchsicht
# ---------------------------------------------------------------------------


def test_prozentzeichen_im_suchtext_trifft_nur_prozentzeichen(
    logged_in_client: TestClient,
) -> None:
    """Verhindert, dass ein „%" im Suchfeld zum Platzhalter für ALLES wird.

    ``ilike(f"%{q}%")`` ohne Maskierung liest jedes ``%`` der Eingabe als
    „beliebig viele Zeichen": die Suche „50% Rabattprobe" traf auch
    „50 irgendwas Rabattprobe" — und die Suche „%" den ganzen Bestand. Genau
    davor soll die Vorschau schützen; sie zeigte in dem Fall selbst die
    aufgeblähte Menge an und bestätigte den Fehler, statt ihn zu melden.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")

    echt = _buchung(konto, "Rabatt 50% Rabattprobe")
    koeder = _buchung(konto, "Rabatt 50 Franken Rabattprobe")

    vorschau = _vorschau_zahl(logged_in_client, q="50% Rabattprobe")
    assert vorschau == 1, (
        "Die Vorschau zählt Buchungen ohne Prozentzeichen mit — „%“ wirkt noch "
        "als Platzhalter."
    )

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": "50% Rabattprobe", "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 200, resp.text
    # Vorschau und Ausführung müssen dieselbe Maskierung benutzen — sonst zeigt
    # die eine etwas anderes an, als die andere anfasst.
    assert _zugeordnet_zahl(resp.text) == vorschau
    assert _kategorie_von(echt) == cat.id
    assert _kategorie_von(koeder) is None, \
        "„%“ wirkt in der Ausführung noch als Platzhalter"


def test_unterstrich_im_suchtext_trifft_nur_unterstriche(
    logged_in_client: TestClient,
) -> None:
    """Wie oben, für den zweiten LIKE-Platzhalter: ``_`` steht für GENAU EIN Zeichen.

    Unauffälliger als ``%`` und deshalb gefährlicher: „Konto_2" traf ohne
    Maskierung auch „Konto 2" und „KontoX2", die Trefferzahl wirkte plausibel.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")

    echt = _buchung(konto, "Zahlung Unterstrichprobe_A")
    koeder = _buchung(konto, "Zahlung UnterstrichprobeXA")

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": "Unterstrichprobe_A", "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 200, resp.text
    assert _kategorie_von(echt) == cat.id
    assert _kategorie_von(koeder) is None, \
        "„_“ wirkt noch als Platzhalter für ein beliebiges Zeichen"


def test_kein_ungeschuetztes_like_muster_im_quelltext() -> None:
    """Dieselbe Konstruktion stand an fünf Stellen — eine reparierte reicht nicht.

    Buchungsfilter, Regel-Lernen, Regel-Gruppen, Abo-Suche und die Suche im
    Zuordnen-Dropdown bauten alle ihr eigenes ``%{eingabe}%``. Wer künftig eine
    sechste dazuschreibt, soll hier stolpern und stattdessen ``enthaelt()``
    nehmen — den einen Ort, an dem maskiert wird.
    """
    muster = re.compile(r"\.i?like\(\s*f['\"]%")
    treffer = []
    for pfad in sorted(SRC.rglob("*.py")):
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            # ``escape=`` ist die Maskierung — genau eine Zeile im Projekt darf
            # das Muster bauen, und das ist enthaelt() selbst.
            if muster.search(zeile) and "escape=" not in zeile:
                treffer.append(f"{pfad.name}:{nr}  {zeile.strip()[:70]}")
    assert not treffer, (
        "Hier wird ein LIKE-Muster aus einer Eingabe zusammengesetzt, ohne die "
        "Platzhalter zu maskieren:\n  " + "\n  ".join(treffer)
        + "\n\nStattdessen moneten.db.models.enthaelt(spalte, text) benutzen."
    )


def test_abo_suche_liest_prozentzeichen_nicht_als_platzhalter() -> None:
    """Zweiter Fundort derselben Konstruktion: „Abo aus Buchungen verbinden".

    Ohne Maskierung lieferte die Suche „%o" dort eine erfundene Abo-Statistik
    aus SÄMTLICHEN Buchungen mit einem „o" im Text — Anzahl, Zeitraum und
    Median-Betrag inklusive.
    """
    from moneten.services.subscriptions import match_transactions

    konto = _konto_id()
    _buchung(konto, "Monatliches Abo Platzhalterprobe", monate_zurueck=0)
    _buchung(konto, "Monatliches Abo Platzhalterprobe", monate_zurueck=1)

    with SessionLocal() as db:
        assert match_transactions(db, "Platzhalterprobe") is not None, \
            "Testaufbau kaputt: die Buchungen sind gar nicht auffindbar"
        assert match_transactions(db, "%o") is None, (
            "„%o“ trifft noch jede Buchung mit einem „o“ im Text — das "
            "Prozentzeichen wirkt als Platzhalter."
        )


def test_unbekannter_buchungstyp_ist_kein_filter(logged_in_client: TestClient) -> None:
    """Verhindert eine Zuweisung auf den ganzen Bestand über einen Tippfehler.

    Die Schutzabfrage hing an den rohen Formularwerten (``any(fa.values())``):
    ``kind=egal`` galt damit als aktiver Filter, erzeugte in
    ``_filter_conditions`` aber keine einzige Bedingung — der Schutz griff nicht,
    und die Zuweisung lief über ALLE Buchungen.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    frei = _buchung(konto, "Unbeteiligte Buchung Typprobe")

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "kind": "egal", "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 400, "Ein unbekannter Buchungstyp gilt noch als Filter"
    assert _kategorie_von(frei) is None


def test_leiste_erscheint_nicht_bei_filtern_ohne_wirkung(
    logged_in_client: TestClient,
) -> None:
    """Verhindert ein Angebot, das der Server garantiert ablehnt.

    Suchtext aus lauter Leerzeichen und ein unbekannter Buchungstyp galten beim
    Aufbau der Seite als „Filter aktiv" — die Leiste erschien also über dem
    ganzen Bestand, und ihr Knopf lief in ein 400. Ein Knopf, der sicher
    scheitert, ist schlimmer als kein Knopf.
    """
    for werte in ({"q": "   "}, {"kind": "egal"}):
        seite = logged_in_client.get("/transactions", params=werte,
                                     headers={"HX-Request": "true"})
        assert 'id="tx-bulk"' not in seite.text, (
            f"Leiste erscheint bei {werte} — dieser „Filter“ schränkt nichts ein."
        )


def test_knopf_bleibt_ohne_zielkategorie_deaktiviert(
    logged_in_client: TestClient,
) -> None:
    """Der zweite sichere Fehlschlag: Zuweisen ohne gewählte Kategorie.

    Der Server antwortet darauf mit 400 („Bitte zuerst eine Zielkategorie
    wählen"). Solange keine gewählt ist, bleibt der Knopf deshalb grau — und die
    Leiste sagt daneben, warum.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Zielkategorieprobe Iota"
    _buchung(konto, f"{marke} Zahlung")

    ohne = _zuweisen_knopf(_leiste(logged_in_client, q=marke))
    assert "disabled" in ohne, (
        "Der Knopf ist ohne Zielkategorie klickbar — der Server lehnt das mit 400 ab."
    )

    mit = _zuweisen_knopf(_leiste(logged_in_client, q=marke,
                                  assign_category_id=str(cat.id)))
    assert "disabled" not in mit, \
        "Der Knopf bleibt grau, obwohl eine Kategorie gewählt ist"


def test_javascript_gibt_den_knopf_bei_auswahl_wieder_frei() -> None:
    """Gegenstück zum vorigen Test: das ``disabled`` muss auch wieder weggehen.

    Die Kategorie wird im Browser gewählt (der Picker schreibt sie ins
    Hidden-Input und feuert ``change``). Ohne Handler dafür wäre der Knopf bis
    zum nächsten Neurendern tot — kein Fortschritt gegenüber dem sicheren
    Fehlschlag, nur ein anderer.
    """
    js = (SRC / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function initBulkGuard(" in js, "Kein Handler, der den Knopf wieder freigibt"
    block = js[js.index("function initBulkGuard("):]
    block = block[:block.index("\n  }")]
    assert "assign_category_id" in block
    assert 'addEventListener("change"' in block, (
        "Der Handler hört nicht auf die Auswahl — der Knopf bliebe bis zum "
        "nächsten Neurendern grau."
    )
    assert "initBulkGuard();" in js.split("function boot()")[1], \
        "initBulkGuard wird beim Seitenaufbau nie aufgerufen"


def test_bestaetigungstext_und_wirkung_meinen_dieselbe_menge(
    logged_in_client: TestClient,
) -> None:
    """Verhindert, dass man eine Zahl bestätigt und eine andere Menge auslöst.

    Der Knopf trug ``hx-include="#tx-filter"`` und las die Filterfelder damit im
    Moment des KLICKS, während der Text der Sicherheitsabfrage aus dem letzten
    Rendern stammte. Dazwischen liegt mindestens die 400-ms-Verzögerung des
    Suchfelds: wer in dieser Zeit weitertippt, bestätigt „3 Buchungen" und ändert
    dreissig. Deshalb reist der Filter jetzt eingefroren im Knopf mit.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Einfrierprobe Tau"
    for i in range(3):
        _buchung(konto, f"{marke} {i}", monate_zurueck=i)

    knopf = _zuweisen_knopf(_leiste(logged_in_client, q=marke,
                                    assign_category_id=str(cat.id)))
    assert "#tx-filter" not in knopf, (
        "Der Knopf liest die Filterfelder noch live (hx-include). Damit kann die "
        "bestätigte Zahl eine andere Menge meinen als der Klick."
    )

    werte = _knopf_werte(knopf)
    assert werte["q"] == marke
    for feld in ("account_id", "category_id", "kind", "only_receipts", "overwrite",
                 "zeitraum"):
        assert feld in werte, f"„{feld}“ reist nicht eingefroren mit"

    # Was der Knopf schickt, muss genau die Menge treffen, die er ankündigt.
    resp = logged_in_client.post("/transactions/assign-filtered",
                                 data={**werte, "assign_category_id": str(cat.id)})
    assert resp.status_code == 200, resp.text
    assert _zugeordnet_zahl(resp.text) == _confirm_zahl(knopf) == 3


def test_kopfzeile_zaehlt_nur_zuweisbare_buchungen(logged_in_client: TestClient) -> None:
    """Verhindert zwei Zahlen übereinander, die dasselbe zu zählen behaupten.

    Die Summenzeile zählt alle Treffer des Filters, die Leiste darunter nur die
    zuweisbaren (ohne aufgeteilte Buchungen und Umbuchungen). Stand über der
    Leiste „N Buchungen im Filter", widersprachen sich die beiden Zeilen direkt —
    und die kleinere Zahl sah aus wie ein Fehler.
    """
    konto = _konto_id()
    marke = "Kopfzeilenprobe My"
    _buchung(konto, f"{marke} normal 1")
    _buchung(konto, f"{marke} normal 2")
    _buchung(konto, f"{marke} Umbuchung", art=ManagementType.TRANSFER)
    _buchung(konto, f"{marke} Sammelbeleg", aufgeteilt=True)

    # `zeitraum=alles` statt des früheren `sum_period=gesamt`: seit dem
    # Heute begrenzt EIN Wert Summenzeile und Liste. Alle vier Buchungen
    # liegen zwar im laufenden Monat, aber der Test soll an dem Tag nicht
    # kippen, an dem der Helfer sein Datum ändert.
    seite = logged_in_client.get("/transactions",
                                 params={"q": marke, "zeitraum": "alles"},
                                 headers={"HX-Request": "true"})
    assert "4 Buchungen" in seite.text, \
        "Testaufbau kaputt: die Summenzeile zählt nicht alle vier Treffer"
    assert re.search(r"<strong>2</strong>\s*zuweisbare Buchungen", seite.text), (
        "Die Kopfzeile der Leiste sagt nicht, dass sie nur die zuweisbaren zählt "
        "— sie widerspricht damit der Summenzeile direkt darüber."
    )


def test_leiste_gibt_es_auch_als_teilansicht_nur_mit_filter(logged_in_client) -> None:
    """Eine Sperre, die nur an einem von drei Eingängen hängt, ist keine.

    Die Vollseite verweigerte die Massen-Leiste ohne wirksamen Filter korrekt —
    die Teilansicht ``/transactions/bulk-bar`` prüfte gar nichts. Wer sie direkt
    aufrief, bekam die Zuweisung auf den GANZEN Bestand angeboten, mit gültigem
    Knopf und passender Sicherheitsabfrage. Gefunden von der Nachprüfung,
    nachdem dieselbe Lücke an der Vollseite bereits geschlossen war.
    """
    ohne = logged_in_client.get("/transactions/bulk-bar")
    assert ohne.status_code == 200
    assert "tx-bulk" not in ohne.text, "Ohne Filter darf keine Leiste kommen"

    leer = logged_in_client.get("/transactions/bulk-bar?q=%20%20%20")
    assert "tx-bulk" not in leer.text, "Reine Leerzeichen sind kein Filter"

    unbekannt = logged_in_client.get("/transactions/bulk-bar?kind=gibtsnicht")
    assert "tx-bulk" not in unbekannt.text, "Ein Wert ohne WHERE-Bedingung ist kein Filter"

    mit = logged_in_client.get("/transactions/bulk-bar?q=Migros")
    assert "tx-bulk" in mit.text, "Mit echtem Filter muss die Leiste erscheinen"


# ---------------------------------------------------------------------------
# Jahres-Vorgabe der Liste: darf die Sammelaktion nur ausdrücklich erreichen
# ---------------------------------------------------------------------------


def _vorjahr() -> int:
    """Monate zurück bis sicher ins Vorjahr — unabhängig davon, wann der Test läuft.

    ``heute.month - 1`` Monate zurück landet im Januar dieses Jahres; fünf
    weitere liegen damit im Juli des Vorjahres.
    """
    return date.today().month + 5


def test_zuweisung_ohne_zeitraumfeld_trifft_weiter_alle_jahre(
    logged_in_client: TestClient,
) -> None:
    """Die Jahres-Vorgabe der Liste darf die Massen-Zuweisung nicht mitnehmen.

    Die Buchungsseite zeigt ohne Parameter nur das laufende Jahr. Wäre dieser
    Vorgabewert auch die Vorgabe von ``/assign-filtered`` und ``/bulk-bar``,
    würde jeder Aufruf, der das Feld nicht kennt, still weniger anfassen als
    verlangt — bei einer Sammelaktion der teure Fehler: man ordnet 60 von 195
    Buchungen zu und merkt es erst in der Jahresauswertung.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Jahresvorgabeprobe Nyx"
    heuer = _buchung(konto, f"{marke} heuer", monate_zurueck=0)
    frueher = _buchung(konto, f"{marke} frueher", monate_zurueck=_vorjahr())

    assert _vorschau_zahl(logged_in_client, q=marke) == 2, \
        "Die Vorschau ohne Zeitraum-Feld zählt schon nicht mehr den ganzen Bestand"

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "q": marke, "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 200, resp.text
    assert _zugeordnet_zahl(resp.text) == 2
    assert _kategorie_von(heuer) == cat.id
    assert _kategorie_von(frueher) == cat.id, (
        "Die Buchung aus dem Vorjahr blieb liegen — ein Anzeige-Vorgabewert hat "
        "die Sammelaktion umdefiniert"
    )


def test_zuweisung_folgt_dem_angezeigten_jahr(logged_in_client: TestClient) -> None:
    """Die Gegenrichtung: zeigt die Liste nur das laufende Jahr, muss die Leiste
    auch davon sprechen — und der Knopf genau das tun.

    Sonst stünde über einer Liste mit einer Zeile ein Knopf, der zwei Buchungen
    ändert. Vorschau, Sicherheitsabfrage und Wirkung müssen dieselbe Menge
    meinen, egal welcher Zeitraum gerade gilt.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Jahresfilterprobe Ody"
    heuer = _buchung(konto, f"{marke} heuer", monate_zurueck=0)
    frueher = _buchung(konto, f"{marke} frueher", monate_zurueck=_vorjahr())

    vorschau = _vorschau_zahl(logged_in_client, q=marke, zeitraum="jahr")
    assert vorschau == 1, "Die Leiste zählt Buchungen mit, die die Liste gar nicht zeigt"

    knopf = _zuweisen_knopf(_leiste(logged_in_client, q=marke, zeitraum="jahr",
                                    assign_category_id=str(cat.id)))
    assert _knopf_werte(knopf)["zeitraum"] == "jahr", \
        "Der Zeitraum reist nicht eingefroren mit — der Knopf trifft eine andere Menge"
    assert _confirm_zahl(knopf) == vorschau

    resp = logged_in_client.post("/transactions/assign-filtered",
                                 data={**_knopf_werte(knopf),
                                       "assign_category_id": str(cat.id)})
    assert resp.status_code == 200, resp.text
    assert _zugeordnet_zahl(resp.text) == vorschau
    assert _kategorie_von(heuer) == cat.id
    assert _kategorie_von(frueher) is None, \
        "Zugeordnet wurde ausserhalb des angezeigten Jahres"


def test_jahr_allein_ist_kein_filter(logged_in_client: TestClient) -> None:
    """„Alles aus 2026" ist keine Auswahl, sondern immer noch der ganze Bestand.

    Der Zeitraum darf die Schutzregel deshalb nicht erfüllen — sonst erschiene
    die Zuweisungs-Leiste schon beim blossen Öffnen der Seite und böte einen
    kompletten Jahrgang auf einen Klick an.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    frei = _buchung(konto, "Unbeteiligte Buchung Jahrprobe", monate_zurueck=0)

    resp = logged_in_client.post("/transactions/assign-filtered", data={
        "zeitraum": "jahr", "assign_category_id": str(cat.id),
    })
    assert resp.status_code == 400, "Ein blosser Zeitraum gilt als Filter"
    assert _kategorie_von(frei) is None

    ohne = logged_in_client.get("/transactions", headers={"HX-Request": "true"})
    assert 'id="tx-bulk"' not in ohne.text, \
        "Die Leiste erscheint durch die Jahres-Vorgabe ohne jeden Filter"
