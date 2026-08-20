"""Ein Beleg-Scan darf nicht aus Versehen abbrechen.

Gemeldet: „wenn eine quittung sehr lange verarbeitet wird oder man auf einer
quittung ausversehen daneben tippt dann verschwindet sie, bricht ab. vorgang
ganz verlohren. schon mehrmals passiert."

Der Scan ist der einzige Vorgang der App, der sich nicht wiederholen lässt:
das Foto wird nur im Speicher gelesen und danach verworfen, die Analyse rechnet
auf dem NAS bis zu zwanzig Sekunden. Was hier verlorengeht, ist weg — der Beleg
müsste neu fotografiert werden, sofern man ihn überhaupt noch hat.

Drei Vorkehrungen, drei Ebenen:

* **Der Dialog schliesst nicht mehr am Hintergrund.** Hinaus geht es über
  „Abbrechen" (und am Desktop über Escape).
* **Während gerechnet oder gespeichert wird, ist gar nichts abbrechbar** —
  keine Taste, keine Geste, und der Browser fragt vor dem Neuladen nach.
* **Das Ergebnis überlebt den Dialog.** Ein Entwurf hält Analyse UND
  Korrekturen fest; er fällt erst weg, wenn der Server den Beleg hat.

``theme.css`` und ``app.js`` werden geparst — ohne Browser. Das fängt nicht
jede Überlagerung, aber jede, die schon einmal passiert ist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import PendingReceipt
from moneten.db.session import SessionLocal

WURZEL = Path(__file__).resolve().parents[1]
STATIC = WURZEL / "src" / "moneten" / "static"
THEME = STATIC / "css" / "theme.css"
APP_JS = STATIC / "js" / "app.js"


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _funktion(name: str) -> str:
    """Rumpf einer Funktion aus app.js (alles bis zur schliessenden Klammer)."""
    js = _js()
    start = js.index(f"function {name}(")
    return js[start : js.index("\n  }", start)]


def _horcher(rumpf: str, ereignis: str) -> str:
    """Der Rumpf des ersten ``addEventListener("<ereignis>", …)`` in ``rumpf``."""
    marke = f'addEventListener("{ereignis}"'
    assert marke in rumpf, f"Kein {ereignis}-Horcher vorhanden"
    rest = rumpf[rumpf.index(marke) :]
    return rest[: rest.index("});")]


def _klammerblock(text: str, ab: int) -> str:
    """Von der ``{`` an Position ``ab`` bis zu ihrer schliessenden Klammer.

    Klammern zählen statt auf die nächste ``}``-Zeile zu hoffen: sonst liesse
    sich eine Anweisung durch eine eingeschobene Klammer optisch im Zweig
    halten, obwohl sie längst dahinter steht.
    """
    tiefe = 0
    for i in range(ab, len(text)):
        if text[i] == "{":
            tiefe += 1
        elif text[i] == "}":
            tiefe -= 1
            if tiefe == 0:
                return text[ab : i + 1]
    raise AssertionError("Klammer wird nicht geschlossen")


def _css_regel(selektor: str) -> dict[str, str]:
    """Deklarationen der Regel, deren Selektorliste ``selektor`` enthält."""
    css = re.sub(r"/\*.*?\*/", "", THEME.read_text(encoding="utf-8"), flags=re.S)
    for sel, rumpf in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selektor not in [t.strip() for t in sel.split(",")]:
            continue
        aus = {}
        for zeile in rumpf.split(";"):
            name, _, wert = zeile.partition(":")
            if wert:
                aus[name.strip()] = wert.strip()
        return aus
    raise AssertionError(f"Keine Regel für {selektor!r} in theme.css")


# ---------------------------------------------------------------------------
# 1. Der Tipp daneben
# ---------------------------------------------------------------------------


def test_ein_tipp_neben_den_dialog_schliesst_ihn_nicht() -> None:
    """Das war der teuerste Bedienfehler der App.

    Der Hintergrund ist die grösste Fläche des Bildschirms; am Handy trifft
    man sie mit dem Daumen im Vorbeigehen. Ein Klick darauf leerte
    ``#kz-modal`` — und damit war die fertige Analyse weg, ohne Rückfrage und
    ohne Weg zurück.
    """
    klick = _horcher(_funktion("initReceiptScan"), "click")
    assert "kz-modal" not in klick, (
        "Der delegierte Klick-Handler reagiert wieder auf #kz-modal — das ist "
        "der Hintergrund. Ein Tipp daneben wirft die Analyse weg."
    )
    assert "kz-saved-close" in klick, (
        "Die Gespeichert-Meldung liesse sich nicht mehr wegklicken."
    )


def test_abbrechen_laesst_sich_zuruecknehmen() -> None:
    """„Abbrechen" ist der Ausgang — aber ein Fehlgriff darauf kostet dasselbe.

    Zwanzig Sekunden Analyse dürfen nicht an einem Daumen hängen, deshalb
    bietet der Toast danach „Zurückholen" an.
    """
    scan = _funktion("initReceiptScan")
    m = re.search(r"cancelBtn\.addEventListener\(\"click\",([^\n]*)", scan)
    assert m, "Der Abbrechen-Knopf ist nicht mehr gebunden"
    assert "undoable: true" in m.group(1), (
        f"Abbrechen verwirft die Analyse ersatzlos: {m.group(1).strip()!r}"
    )
    schliessen = _funktion("kzCloseModal")
    assert "actionLabel" in schliessen and "kzRestoreDraft" in schliessen, (
        "kzCloseModal bietet kein Zurückholen an — dann ist `undoable` folgenlos."
    )


# ---------------------------------------------------------------------------
# 2. Während gerechnet wird, gibt es kein Zurück
# ---------------------------------------------------------------------------


def test_escape_ist_der_zweite_ausgang() -> None:
    """Am Desktop erwartet man die Taste; ohne sie gäbe es dort nur den Knopf."""
    taste = _horcher(_funktion("initReceiptScan"), "keydown")
    assert "Escape" in taste and "kzCloseModal(" in taste, (
        "Escape schliesst den Beleg-Dialog nicht mehr."
    )


def test_escape_greift_nicht_waehrend_gerechnet_wird() -> None:
    """Sonst bliebe genau ein Weg offen, den Vorgang mittendrin zu verlieren.

    Die Sperrschicht deckt die Fläche ab — die Tastatur liegt darüber hinweg.
    """
    taste = _horcher(_funktion("initReceiptScan"), "keydown")
    assert "kzBusy()" in taste, (
        "Der Escape-Handler fragt nicht mehr, ob gerade gerechnet wird."
    )
    assert taste.index("kzBusy()") < taste.index("kzCloseModal("), (
        "Die Busy-Abfrage steht hinter dem Schliessen — sie kommt zu spät."
    )


def test_beide_teuren_schritte_teilen_dieselbe_sperre() -> None:
    """Analysieren und Speichern sind derselbe Fall: eine laufende Rechnung.

    Vorher hatte nur der Upload eine Anzeige, und die war reine Kosmetik —
    das Speichern lief ohne jede Sperre.
    """
    for funktion in ("initReceiptPhotoInputs", "initReceiptScan"):
        assert "kzSetBusy(true" in _funktion(funktion), (
            f"{funktion} setzt die Sperre nicht — dort ist der Vorgang wieder "
            f"durch Danebentippen unterbrechbar."
        )


def test_waehrend_gerechnet_wird_erstickt_die_sperrschicht_jede_geste() -> None:
    """Chromes Ziehen-zum-Neuladen ist der zweite Weg, einen Scan zu verlieren.

    Es braucht keinen Knopf und keine Absicht: ein Wischen nach unten genügt,
    und die Seite lädt neu, während das Foto nur im Speicher liegt.
    """
    assert _css_regel(".kz-analyzing.htmx-request").get("touch-action") == "none", (
        ".kz-analyzing.htmx-request lässt Gesten wieder durch."
    )


def test_die_positionsliste_zieht_die_seite_nicht_mit() -> None:
    """Am Ende der Liste weiterzuziehen ist dieselbe Geste — nur im Dialog."""
    assert _css_regel(".kz").get("overscroll-behavior") == "contain", (
        "Das Beleg-Papier gibt das Überscrollen an die Seite weiter."
    )


def test_der_ausgang_ist_lesbar_beschriftet() -> None:
    """„Abbrechen" ist der einzige Weg aus dem Dialog — er muss als Wort dastehen.

    Bei 375px teilen sich beide Knöpfe 313px, und das global gesetzte
    ``overflow-wrap: anywhere`` drückt die Mindestbreite eines Knopfes auf EIN
    Zeichen: nachgemessen blieben „Abbrechen" 113px bei 120px Textbreite, und
    der Knopf las sich „Abbreche / n". Umbrechen soll die Leiste, nicht das
    Wort — derselbe Befund wie an der Lohn-Knopfleiste.
    """
    css = re.sub(r"/\*.*?\*/", "", THEME.read_text(encoding="utf-8"), flags=re.S)
    assert "overflow-wrap: anywhere" in css, (
        "Der globale Umbruch ist weg — dann ist diese Gegenregel unnötig "
        "geworden und gehört entfernt statt stehengelassen."
    )
    assert _css_regel(".kz-actions .btn").get("overflow-wrap") == "normal", (
        "Die Knöpfe des Beleg-Editors brechen wieder mitten im Wort um."
    )
    assert _css_regel(".kz-actions").get("flex-wrap") == "wrap", (
        "Ohne Umbruch der Leiste bleibt für den zweiten Knopf zu wenig übrig."
    )


def test_der_browser_fragt_nach_bevor_er_mitten_im_scan_neu_laedt() -> None:
    """Die letzte Bremse — sie greift auch dort, wo die App nichts sieht
    (Neuladen-Knopf, Zurück-Geste, Tab schliessen)."""
    busy = _funktion("kzSetBusy")
    assert 'addEventListener("beforeunload"' in busy, (
        "Ein Neuladen mitten in der Analyse geht wieder kommentarlos durch."
    )
    assert 'removeEventListener("beforeunload"' in busy, (
        "Die Rückfrage bleibt nach dem Scan hängen und nervt auf jeder Seite."
    )


# ---------------------------------------------------------------------------
# 3. Das Ergebnis überlebt den Dialog
# ---------------------------------------------------------------------------


def test_die_analyse_wird_bei_jeder_aenderung_gesichert() -> None:
    """Nur beim Öffnen zu sichern hiesse: die Analyse überlebt, die Korrekturen
    daran nicht. Gerade die kosten die Handarbeit."""
    js = _js()
    start = js.index("    function render() {")
    render = js[start : js.index("\n    }", start)]
    assert "kzSaveDraft(" in render, (
        "render() legt keinen Entwurf mehr an — nach einem Neuladen wäre der "
        "Beleg neu zu fotografieren."
    )


def test_ein_unterbrochener_scan_kommt_beim_naechsten_laden_zurueck() -> None:
    """Und NUR beim Laden der Seite.

    Nach einem HTMX-Swap wieder aufzuspringen hiesse: der Dialog geht bei
    jedem nachgeladenen Monat erneut auf.
    """
    js = _js()
    boot = js[js.index("  function boot() {") : js.index("\n  }", js.index("  function boot() {"))]
    assert "kzRestoreDraft()" in boot, (
        "boot() holt den unterbrochenen Scan nicht zurück."
    )
    nach_swap = js[js.index('document.addEventListener("htmx:afterSwap"') :]
    nach_swap = nach_swap[: nach_swap.index("\n  });")]
    assert "kzRestoreDraft" not in nach_swap, (
        "Der Entwurf wird nach jedem HTMX-Swap wiederhergestellt — der Dialog "
        "springt dann ungefragt auf."
    )


def test_der_entwurf_faellt_erst_weg_wenn_der_server_ihn_hat() -> None:
    """Ein abgewiesenes Speichern ist kein gespeicherter Beleg.

    Fiele der Entwurf schon beim Absenden weg, wäre die Analyse bei jedem
    Fehlschlag verloren — also genau dann, wenn man sie noch braucht.
    """
    scan = _funktion("initReceiptScan")
    start = scan.index('confirmBtn.addEventListener("click"')
    bestaetigen = scan[start : scan.index("\n    });", start)]
    assert bestaetigen.count("kzDropDraft()") == 1, (
        "kzDropDraft() steht mehr als einmal im Bestätigen-Zweig — dann sagt "
        "die Stelle nichts mehr darüber aus, wann der Entwurf wegfällt."
    )
    zweig = _klammerblock(bestaetigen, bestaetigen.index("{", bestaetigen.index("if (r.ok)")))
    assert "kzDropDraft()" in zweig, (
        "Der Entwurf wird ausserhalb des Erfolgszweigs verworfen — damit auch "
        "dann, wenn der Server den Beleg abgelehnt hat."
    )


def test_ein_alter_entwurf_geht_von_selbst() -> None:
    """Nach einem Tag ist es kein unterbrochener Vorgang mehr, sondern ein
    vergessener — ein Dialog, der dann aufspringt, wäre Belästigung."""
    lesen = _funktion("kzReadDraft")
    assert "KZ_DRAFT_MAX_AGE" in lesen and "kzDropDraft()" in lesen, (
        "kzReadDraft() gibt beliebig alte Entwürfe zurück."
    )


# ---------------------------------------------------------------------------
# 4. Was der Server dazu sagt — daran liest app.js ab, ob der Entwurf weg darf
# ---------------------------------------------------------------------------

# Erfundener Beleg, erkennbar erfunden.
_BELEG = {
    "merchant": "Testladen Musterhausen",
    "date": "2026-08-01",
    "amount": "12.30",
    "items": [{"name": "Beispielware", "price": "12.30"}],
}


def test_ein_leerer_beleg_wird_abgewiesen_statt_still_verworfen(
    logged_in_client: TestClient,
) -> None:
    """Weder Positionen noch Betrag: hier wurde NICHTS gespeichert.

    Mit 200 hätte der Editor den Entwurf weggeworfen und der Nutzer stünde vor
    einer Meldung statt vor seinem Beleg — obwohl bloss das Total fehlt.
    """
    antwort = logged_in_client.post(
        "/import/receipts/photo/confirm", data={"data": json.dumps({"items": []})}
    )
    assert antwort.status_code == 422, (
        f"Ein leerer Beleg meldet {antwort.status_code} — app.js liest daran ab, "
        f"ob es den Entwurf verwerfen darf."
    )


def test_ein_angenommener_beleg_meldet_sich_mit_200(
    logged_in_client: TestClient,
) -> None:
    """Die Gegenprobe zur Regel oben: 200 heisst, der Beleg liegt beim Server."""
    antwort = logged_in_client.post(
        "/import/receipts/photo/confirm",
        data={"data": json.dumps(_BELEG), "ocr_text": "Testladen Musterhausen 12.30"},
    )
    assert antwort.status_code == 200, antwort.text
    with SessionLocal() as db:
        vorgemerkt = db.scalars(
            select(PendingReceipt).where(PendingReceipt.merchant == _BELEG["merchant"])
        ).all()
    assert vorgemerkt, "200 gemeldet, aber nichts vorgemerkt — die Meldung wäre gelogen."
