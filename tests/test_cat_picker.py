"""Kategorie-Picker: ein gemeinsames Panel statt eines Panels je Auslöser.

Der Fehler, um den es hier geht, kam vom Roundtrip. Ein Klick auf die Kategorie
einer Buchungszeile holte die Liste komplett neu (``GET /transactions?quickcat=<id>``),
und der Server rendert diese eine Zeile mit aufgeklapptem Picker. „Offen" stand
damit NUR im URL-Parameter — jedes weitere Neurendern von ``#transactions-root``
(nachlaufende Suche nach 400 ms, Filterwechsel, zweiter Klick) lieferte die Liste
ohne ``quickcat`` und riss den Picker weg. Beim Nutzer mit 2512 Buchungen dauert
der Roundtrip lange genug, dass das ständig eintrat; lokal mit 81 Zeilen und
15 ms war es nicht zu sehen.

Die Tests sichern deshalb nicht „das Panel geht auf", sondern die Struktur, die
das Zufallen unmöglich macht: das Panel steht genau einmal pro Seite und liegt
ausserhalb von allem, was HTMX neu rendert.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Category, Transaction
from moneten.db.session import SessionLocal

APP_JS = Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "js" / "app.js"

_zaehler = {"n": 0}

# class="cat-picker" bzw. class="cat-picker …" — NICHT cat-picker-group/-search/
# -panel, die im Panel selbst stecken und die Zählung sonst verfälschen.
_AUSLOESER = re.compile(r'class="cat-picker[ "]')


def _konto_id() -> int:
    """Frisches, isoliertes Konto — die Test-DB wird von vielen Modulen geteilt."""
    _zaehler["n"] += 1
    with SessionLocal() as db:
        acc = Account(name=f"Picker-Test {_zaehler['n']}", type=AccountType.BANK,
                      currency="CHF", opening_balance=Decimal("0"),
                      current_balance=Decimal("0"), sort_order=900)
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
    """Datum n Monate vor heute, immer am 15. — das Fenster der Liste zählt in
    Monatskarten (``MONTH_WINDOW``), nicht in Tagen."""
    heute = date.today()
    monat, jahr = heute.month - n, heute.year
    while monat < 1:
        monat += 12
        jahr -= 1
    return date(jahr, monat, 15)


def _buchung(konto_id: int, beschreibung: str, *, monate_zurueck: int = 0) -> int:
    with SessionLocal() as db:
        tx = Transaction(account_id=konto_id, category_id=None,
                         date=_monate_zurueck(monate_zurueck), amount=Decimal("-20.00"),
                         description=beschreibung)
        db.add(tx)
        db.commit()
        return tx.id


def _kategorie_von(tx_id: int) -> int | None:
    with SessionLocal() as db:
        return db.get(Transaction, tx_id).category_id


# ---------------------------------------------------------------------------


def test_nur_ein_panel_im_dom_egal_wie_viele_zeilen(logged_in_client: TestClient) -> None:
    """Verhindert den Rückfall auf „Panel je Zeile".

    Das war die naheliegende Lösung und ist unbezahlbar: 54 Kategorien × 81
    Zeilen sind nachgerechnet 1136 KB Markup pro Seite. Genau weil das nicht ging,
    gab es überhaupt den Roundtrip, an dem der Picker zufiel.
    """
    konto = _konto_id()
    marke = "Panelprobe Einzelstueck"
    for i in range(40):
        _buchung(konto, f"{marke} {i}")

    seite = logged_in_client.get("/transactions", params={"q": marke})
    assert seite.status_code == 200, seite.text
    zeilen = seite.text.count('data-picker-key="tx-')
    assert zeilen >= 40, f"Testaufbau kaputt: nur {zeilen} Zeilen mit Auslöser"
    assert seite.text.count("cat-picker-panel") == 1
    assert seite.text.count('id="cat-panel"') == 1


def test_picker_ueberlebt_das_neurendern_der_liste(logged_in_client: TestClient) -> None:
    """DER eigentliche Fehler: ein Neurendern der Liste schloss den Picker.

    Solange das Panel im ausgetauschten Teilbaum steckte, konnte kein noch so
    gutes JS es retten — HTMX ersetzt ``#transactions-root`` samt Inhalt. Deshalb
    prüft dieser Test die Struktur: die HTMX-Antwort, die die Liste ersetzt,
    bringt Auslöser mit, aber KEIN Panel. Was sie nicht mitbringt, kann sie auch
    nicht zerstören.
    """
    konto = _konto_id()
    marke = "Neurenderprobe Bestand"
    _buchung(konto, marke)

    teil = logged_in_client.get("/transactions", params={"q": marke},
                                headers={"HX-Request": "true"})
    assert teil.status_code == 200, teil.text
    assert _AUSLOESER.search(teil.text), "Die neu gerenderte Liste hat gar keinen Picker-Auslöser"
    assert "cat-picker-panel" not in teil.text, (
        "Die HTMX-Antwort für #transactions-root enthält ein Kategorie-Panel. "
        "Damit räumt jedes Neurendern (nachlaufende Suche, Filterwechsel) den "
        "offenen Picker wieder weg — genau der gemeldete Fehler."
    )
    assert 'id="cat-panel"' not in teil.text


def test_zeile_klappt_ohne_server_roundtrip_auf(logged_in_client: TestClient) -> None:
    """Der Auslöser in der Zeile darf nichts mehr vom Server holen.

    Hielte sich hier ein ``hx-get`` (früher ``?quickcat=<id>``), wäre der Zustand
    „offen" wieder eine Server-Angelegenheit — und die nächste Antwort für
    denselben Container würde ihn wieder verlieren.
    """
    from moneten.routers.transactions import transactions_page

    konto = _konto_id()
    marke = "Roundtripprobe Direkt"
    tx_id = _buchung(konto, marke)

    seite = logged_in_client.get("/transactions", params={"q": marke})
    block = re.search(
        rf'<div class="cat-picker cat-picker-inline" data-picker-key="tx-{tx_id}".*?</div>',
        seite.text, re.S)
    assert block, f"Kein Zeilen-Auslöser gefunden:\n{seite.text[:600]}"
    knopf = block.group(0)[block.group(0).index("<button"):]
    assert "hx-get" not in knopf, f"Der Zeilen-Knopf holt immer noch etwas vom Server:\n{knopf}"

    assert "quickcat" not in seite.text
    assert "quickcat" not in transactions_page.__code__.co_varnames, (
        "Die Route kennt quickcat noch — halbe Entfernungen haben in diesem "
        "Projekt schon zweimal sichtbare Reste hinterlassen."
    )


def test_auswahl_loest_weiterhin_das_speichern_aus(logged_in_client: TestClient) -> None:
    """Der Vertrag zum Server darf sich nicht ändern: change am Hidden-Input.

    Das Panel ist neu, die Übergabe nicht — das Hidden-Input der Zeile trägt
    weiterhin ``hx-post`` auf ``/transactions/<id>/category`` mit
    ``hx-trigger="change"``, und die Route setzt die Kategorie.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Speicherprobe Vertrag"
    tx_id = _buchung(konto, marke)

    seite = logged_in_client.get("/transactions", params={"q": marke})
    # Gezielt das Feld DIESER Zeile — die Massen-Zuweisungs-Leiste oben benutzt
    # denselben Feldnamen und stünde sonst als erster Treffer im Weg.
    feld = re.search(
        rf'data-picker-key="tx-{tx_id}".*?(<input type="hidden" name="assign_category_id"[^>]*>)',
        seite.text, re.S)
    assert feld, "Kein Hidden-Input für die Zuweisung in der Zeile"
    assert f'hx-post="/transactions/{tx_id}/category"' in feld.group(1)
    assert 'hx-trigger="change"' in feld.group(1)

    resp = logged_in_client.post(f"/transactions/{tx_id}/category",
                                 data={"assign_category_id": str(cat.id), "q": marke})
    assert resp.status_code == 200, resp.text
    assert _kategorie_von(tx_id) == cat.id


def test_zugewiesene_zeile_bleibt_nach_dem_speichern_sichtbar(
    logged_in_client: TestClient,
) -> None:
    """Nach der Zuweisung muss die bearbeitete Zeile in der Antwort stehen.

    Die Liste rendert nur die neuesten Monate mit Treffern; ordnet man aus einer
    nachgeladenen, älteren Monatskarte heraus zu, fiele die Zeile ohne
    ``keep_visible`` aus dem Fenster — man klickt, und die Buchung ist weg.
    """
    konto = _konto_id()
    cat = _kategorie("Hobby")
    marke = "Fensterprobe Alt"
    ids = [_buchung(konto, f"{marke} Monat {m}", monate_zurueck=m) for m in range(8)]

    # Beweis, dass der Fall überhaupt greift: die älteste Zeile ist nicht gerendert.
    seite = logged_in_client.get("/transactions", params={"q": marke},
                                 headers={"HX-Request": "true"})
    assert f"{marke} Monat 7" not in seite.text, \
        "Testaufbau kaputt: die älteste Buchung steht doch auf der ersten Seite"

    resp = logged_in_client.post(f"/transactions/{ids[7]}/category",
                                 data={"assign_category_id": str(cat.id), "q": marke})
    assert resp.status_code == 200, resp.text
    assert f"{marke} Monat 7" in resp.text, (
        "Die eben zugeordnete Buchung fehlt in der Antwort — sie ist aus dem "
        "Monatsfenster gefallen."
    )


# GET-Routen, die keine Seite sind, sondern etwas tun. /logout beendet die
# Sitzung — würde der Test sie aufrufen, kämen alle folgenden Seiten als
# Weiterleitung zurück und die Prüfung liefe still ins Leere.
_KEINE_SEITE = {"/logout"}


def _seiten_pfade() -> list[str]:
    """Alle parameterlosen GET-Pfade der App — aus den registrierten Routen.

    Vorher stand hier eine handgepflegte Liste von acht Pfaden. Die altert
    lautlos: eine neue Seite mit Kategorie-Picker (oder eine, die das Panel
    versehentlich mitschleppt) wäre schlicht nicht dabei gewesen.
    """
    from fastapi.routing import APIRoute

    from moneten.main import app

    return [
        r.path for r in app.routes
        if isinstance(r, APIRoute) and "GET" in r.methods
        and "{" not in r.path and r.path not in _KEINE_SEITE
    ]


def test_jede_seite_mit_picker_hat_genau_ein_panel(logged_in_client: TestClient) -> None:
    """Panel und Auslöser müssen zusammen auftreten — in beide Richtungen.

    Fehlt das Panel, tut ein Klick auf den Picker gar nichts (kein Fehler, keine
    Meldung — die schlimmste Sorte). Steht es auf einer Seite ohne Auslöser,
    schleppt sie rund 14 KB Pillen mit, die niemand aufklappen kann; /quick zeigt
    die Kategorien mit einem eigenen Raster und braucht das Panel nicht.

    Geprüft werden alle Vollseiten, die die App registriert — erkennbar am
    ``<html>``: HTMX-Teilantworten (z.B. ``/transactions/bulk-bar``) bringen
    Auslöser ohne Panel mit, und das ist dort gerade richtig so.
    """
    geprueft = []
    for pfad in _seiten_pfade():
        seite = logged_in_client.get(pfad)
        if seite.status_code != 200 or "<html" not in seite.text[:500].lower():
            continue  # Teilantwort, Datei, JSON oder Weiterleitung — keine Seite
        geprueft.append(pfad)
        hat_ausloeser = bool(_AUSLOESER.search(seite.text))
        panels = seite.text.count('id="cat-panel"')
        assert panels == (1 if hat_ausloeser else 0), (
            f"{pfad}: {panels} Panel(s) bei "
            f"{'vorhandenen' if hat_ausloeser else 'keinen'} Auslösern"
        )
    # Sonst könnte die Ableitung kaputtgehen (falsches Filterkriterium), ohne
    # dass ein einziger Test rot wird — die Liste wäre einfach leer.
    assert len(geprueft) >= 8, f"Nur {len(geprueft)} Seiten geprüft: {geprueft}"
    assert "/transactions" in geprueft and "/quick" in geprueft


def test_enter_im_leeren_suchfeld_waehlt_keine_kategorie() -> None:
    """Verhindert, dass ein Enter ohne einen einzigen Tastendruck etwas speichert.

    Der Enter-Zweig nahm die Rückgabe von ``catFilter`` — und die war die erste
    sichtbare Pille, auch bei LEERER Eingabe. Wer das Panel öffnete und Enter
    drückte (naheliegend: „ich will hier nichts, weg damit"), setzte damit still
    die erste Kategorie der Liste an seiner Buchung; in der Buchungsliste hängt
    daran ein ``hx-post``, das sofort speichert.

    Enter darf nur wählen, wenn wirklich gefiltert wurde UND genau eine
    Kategorie übrig bleibt.
    """
    js = APP_JS.read_text(encoding="utf-8")
    # Nur der Tastatur-Handler DES PANELS — „Enter" kommt in app.js mehrfach vor.
    handler = js[js.index('search.addEventListener("keydown"'):]
    zweig = re.search(r'if \(e\.key === "Enter"\) \{(.*?)\} else if', handler, re.S)
    assert zweig, "Kein Enter-Zweig im Suchfeld-Handler gefunden"
    assert "catFilter(" not in zweig.group(1), (
        "Enter nimmt weiterhin direkt das Ergebnis von catFilter — das ist auch "
        "bei leerer Eingabe die erste Kategorie der Liste."
    )
    assert "catEindeutigerTreffer(" in zweig.group(1)

    fn = js[js.index("function catEindeutigerTreffer("):]
    fn = fn[:fn.index("\n  }")]
    assert "anzahl === 1" in fn, "Enter wählt auch bei mehreren Treffern (nicht eindeutig)"
    assert "roh &&" in fn or "roh?" in fn, "Enter wählt auch ohne Eingabe (nicht gefiltert)"


def test_panel_haengt_sich_nicht_an_einen_unsichtbaren_ausloeser() -> None:
    """Verhindert ein Panel, das über der Seite schwebt und auf nichts zeigt.

    Nach dem Neurendern sucht das Panel seinen Auslöser über ``data-picker-key``
    wieder. Geprüft wurde nur, ob das Element EXISTIERT — bei einer zugeklappten
    Monatskarte steht die Zeile aber weiterhin im DOM, nur unsichtbar. Das Panel
    blieb dann offen, ohne dass man sah, für welche Buchung es gilt; die nächste
    Auswahl traf eine Zeile, die man gar nicht mehr vor sich hatte.
    """
    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("if (panel && !panel.hidden) {"):]
    block = block[:block.index("catClose(); }") + 13]
    assert "catSichtbar(" in block, (
        "Die Wiederanknüpfung prüft nur die Existenz des Auslösers, nicht seine "
        "Sichtbarkeit."
    )

    fn = js[js.index("function catSichtbar("):]
    fn = fn[:fn.index("\n  }")]
    assert "checkVisibility" in fn, (
        "catSichtbar benutzt nicht checkVisibility(). Am laufenden Server "
        "nachgemessen: bei einer zugeklappten Monatskarte (geschlossenes "
        "<details>) melden getClientRects(), offsetParent und "
        "getBoundingClientRect() weiterhin eine Box — nur checkVisibility() "
        "sagt die Wahrheit."
    )


def test_panel_fuehrt_jede_kategorie_genau_einmal(logged_in_client: TestClient) -> None:
    """Doppelte Pillen wären der stille Beweis für ein zweites Panel im DOM.

    Zwei Panels heisst: zwei Elemente mit derselben id, und app.js bedient nur
    das erste — der Picker wirkte dann je nach Einsatzort tot.
    """
    seite = logged_in_client.get("/transactions")
    ids = re.findall(r'class="cat-pill" data-cat="(\d+)"', seite.text)
    assert ids, "Das Panel führt gar keine Kategorien"
    assert len(ids) == len(set(ids)), "Mindestens eine Kategorie steht doppelt im Panel"
