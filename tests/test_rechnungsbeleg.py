"""Die Beispielfunk-Rechnung als Beleg an der Buchung: erkennen, zuordnen, zeigen.

**Jeder Text, jeder Betrag und jeder Name in dieser Datei ist erfunden.** Aus dem
Bestand stammt allein das Layout — die Abschnittsnamen, die Spaltenordnung, die
Stelle des Rechnungskopfs. Die Zahlen sind runde Erfindungen, an denen sich die
Summe im Kopf nachrechnen lässt.

Die Rechnungen entstehen hier als **echte PDFs mit Wortkoordinaten**: der
Spaltentext ist das Bindeglied zwischen Datei und Parser, und ein Test, der ihn
überspringt, prüft die Kette nicht, um die es geht.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.config import settings
from moneten.db.models import Account, AccountType, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services import rechnungsbeleg
from moneten.services.receipt_match import (
    KEIN_ZAHLUNGSBELEG,
    attach_receipt,
    auto_match,
    build_suggestions,
    read_receipt_data,
)

# Spalten der Positionstabelle: Bezeichnung, Menge, Preis pro Einheit, Betrag.
# Die Abstände sind grosszügig — geprüft wird hier die Zuordnung, nicht die
# Schwelle der Spaltentrennung (das tut ``test_beispielfunk.py`` an den echten
# Koordinaten).
_SPALTEN_X = (60.0, 300.0, 400.0, 480.0)
_ZEILE_Y = 14.0


def _pdf(ziel: Path, zeilen: list[tuple[str, ...]]) -> Path:
    """Ein PDF, dessen Zellen in den Spalten einer Rechnung stehen.

    Die letzte Zelle einer Zeile landet immer in der Betragsspalte — dort steht
    sie auf der Rechnung, und genau die liest der Parser als Betrag.
    """
    import fitz

    doc = fitz.open()
    seite = doc.new_page()
    y = 40.0
    for zeile in zeilen:
        xs = [_SPALTEN_X[0], *_SPALTEN_X[len(_SPALTEN_X) - len(zeile) + 1:]]
        for x, zelle in zip(xs, zeile, strict=True):
            seite.insert_text((x, y), zelle, fontsize=8)
        y += _ZEILE_Y
    doc.save(str(ziel))
    doc.close()
    return ziel


# Erfundene Positionen: 80.00 + 10.00 − 20.00 = 70.00, mit Rundung −0.05 → 69.95.
_POSITIONEN = (("blue Mobile M", "80.00"), ("Connect Pack", "10.00"),
               ("Kombi-Rabatt", "-20.00"))
_RUNDUNG = "-0.05"


def _summe(positionen=_POSITIONEN, rundung: str = _RUNDUNG) -> Decimal:
    return sum((Decimal(p) for _, p in positionen), Decimal("0")) + Decimal(rundung)


def _zeilen(monat: str, positionen=_POSITIONEN, rundung: str = _RUNDUNG) -> list[tuple[str, ...]]:
    """Eine erfundene Monatsrechnung als Tabellenzeilen."""
    return [
        ("Beispielfunk (Schweiz) AG",),
        (f"Rechnung für {monat} 2031",),
        ("Rechnungstotal inkl. MWST", str(_summe(positionen, rundung))),
        ("Positionen im Detail",),
        ("Menge", "Preis pro Einheit", "Betrag"),
        ("Grundgebühren", positionen[0][1]),
        *[(name, "1", preis, preis) for name, preis in positionen],
        ("Summe", str(_summe(positionen, "0"))),
        ("Rundungsdifferenz", rundung),
    ]


def _spaltentext(zeilen: list[tuple[str, ...]]) -> str:
    """Dieselben Zeilen ohne Umweg über das PDF — für die reinen Textprüfungen."""
    return "\n".join("\t".join(z) for z in zeilen)


def _konto() -> int:
    with SessionLocal() as db:
        acc = Account(name="Beispielfunk-Beleg-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=910)
        db.add(acc)
        db.commit()
        return acc.id


def _buchung(tag: date, betrag: str, text: str = "Beispielfunk Rechnung") -> int:
    with SessionLocal() as db:
        tx = Transaction(account_id=_konto(), date=tag, amount=Decimal(betrag), description=text)
        db.add(tx)
        db.commit()
        return tx.id


def _abgleich(ordner: Path) -> int:
    """Auto-Abgleich über genau diesen Quittungs-Ordner."""
    alt = settings.receipts_dir
    settings.receipts_dir = ordner
    try:
        with SessionLocal() as db:
            return auto_match(db)
    finally:
        settings.receipts_dir = alt


def _anhang(tx_id: int) -> Attachment | None:
    with SessionLocal() as db:
        return db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))


# --------------------------------------------------- Erkennen und strukturieren


def test_rechnung_wird_am_inhalt_erkannt_nicht_am_dateinamen(tmp_path: Path) -> None:
    """Der Dateiname sagt nichts über den Inhalt — er lässt sich ändern.

    Die Datei heisst hier bewusst nach gar nichts. Hinge die Erkennung am Namen,
    fiele jede umbenannte Rechnung aus dem Verfahren.
    """
    _pdf(_pdf_ziel := tmp_path / "dokument_ohne_aussagekraft.pdf", _zeilen("März"))
    befund = rechnungsbeleg.rechnung_aus_datei(str(_pdf_ziel))
    assert befund is not None
    assert befund.period_start == date(2031, 3, 1)
    assert befund.value == Decimal("69.95")


def test_der_anhang_traegt_die_positionen_mit_ihrem_vorzeichen() -> None:
    """Ein Rabatt ist negativ. Als Höhe geschrieben stünde er in der Aufstellung
    wie ein weiteres Abonnement."""
    befund = rechnungsbeleg.rechnung_aus_text(_spaltentext(_zeilen("März")))
    daten = rechnungsbeleg.strukturiert(befund)
    assert daten["merchant"] == "Beispielfunk AG"
    assert daten["amount"] == "69.95"
    assert {p["name"]: p["price"] for p in daten["items"]} == {
        "blue Mobile M": "80.00", "Connect Pack": "10.00", "Kombi-Rabatt": "-20.00",
    }


def test_der_beleg_weist_aus_dass_er_gelesen_und_nicht_geraten_ist() -> None:
    """Die Herkunft steht im Anhang und erscheint im Beleg-Fenster.

    Als „ocr" ausgewiesen sähe dieser Beleg aus wie ein abfotografierter Bon,
    dessen Beträge geschätzt sind — und die Aufstellung an der Buchung, die es
    nur für geprüfte Positionen gibt, hinge an einer Behauptung.
    """
    befund = rechnungsbeleg.rechnung_aus_text(_spaltentext(_zeilen("März")))
    assert rechnungsbeleg.strukturiert(befund)["method"] == "rechnung"


def test_rechnung_die_nicht_aufgeht_ist_kein_beleg() -> None:
    """Der Parser wirft dann — für den Verlauf ist das ein Fehler, den jemand
    sehen muss. Für den Beleg heisst es schlicht: nicht verlässlich genug.

    Ein Anhang, dessen Positionen den gebuchten Betrag NICHT ergeben, behauptete
    eine Genauigkeit, die er nicht hat.
    """
    zeilen = [z for z in _zeilen("März") if z[0] != "Connect Pack"]
    assert rechnungsbeleg.rechnung_aus_text(_spaltentext(zeilen)) is None


def test_eine_extraktion_fuer_skript_und_app() -> None:
    """Skript und App lesen dieselbe Rechnung — mit derselben Funktion.

    Zwei Kopien der Koordinatenlogik würden auseinander driften, und die
    Schwelle, ab der eine neue Spalte beginnt, ist genau die Art Zahl, die nur
    an einer Stelle stehen darf.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import verlaeufe_aus_scans as skript

    from moneten.services.pdf_spalten import pdf_spalten

    assert skript.pdf_spalten is pdf_spalten


# ------------------------------------------------------------------ Zuordnung


def test_rechnung_wird_zum_beleg_an_der_zahlung(tmp_path: Path) -> None:
    """Der Regelfall: die Zahlung liegt rund vier Wochen nach dem Rechnungskopf.

    Zugeordnet wird über den GELESENEN Rechnungsbetrag — nicht über einen aus
    dem Belegtext geschätzten.
    """
    _pdf(tmp_path / "beispielfunk_maerz.pdf", _zeilen("März"))
    tx_id = _buchung(date(2031, 4, 2), "-69.95")

    assert _abgleich(tmp_path) == 1
    att = _anhang(tx_id)
    assert att is not None
    assert att.original_name == "beispielfunk_maerz.pdf"
    # Die Datei bleibt, wo sie ist — verwiesen wird nur.
    assert att.file_path == str(tmp_path / "beispielfunk_maerz.pdf")
    daten = json.loads(att.parsed_items_json)
    assert daten["amount"] == "69.95"
    assert len(daten["items"]) == 3


def test_zahlung_vor_der_monatsmitte_gehoert_zur_vormonatsrechnung(tmp_path: Path) -> None:
    """Anfang Monat wird die Rechnung des VORMONATS bezahlt.

    Bei gleichbleibendem Abonnement trägt diese Zahlung denselben Betrag — ohne
    den frühen Rand des Zahlungsfensters griffe die Rechnung nach der falschen
    der beiden Buchungen, und beide Monate wären danach falsch belegt.
    """
    _pdf(tmp_path / "beispielfunk_april.pdf", _zeilen("April"))
    tx_id = _buchung(date(2031, 4, 3), "-69.95", "Beispielfunk Vormonat")

    assert _abgleich(tmp_path) == 0
    assert _anhang(tx_id) is None


def test_zahlung_lange_nach_dem_fenster_gehoert_zur_folgerechnung(tmp_path: Path) -> None:
    """Der späte Rand hält die Zahlung der nächsten Rechnung draussen."""
    _pdf(tmp_path / "beispielfunk_mai.pdf", _zeilen("Mai"))
    tx_id = _buchung(date(2031, 7, 1), "-69.95", "Beispielfunk viel spaeter")

    assert _abgleich(tmp_path) == 0
    assert _anhang(tx_id) is None


def test_zwei_gleich_hohe_zahlungen_im_fenster_bleiben_offen(tmp_path: Path) -> None:
    """Ist die Zuordnung nicht eindeutig, wird nicht geraten.

    Welche der beiden gleich hohen Abbuchungen zu diesem Monat gehört, weiss die
    Rechnung nicht. Eine falsch zugeordnete Quittung ist schlimmer als eine
    offene: sie sieht aus wie eine Antwort.
    """
    _pdf(tmp_path / "beispielfunk_juni.pdf", _zeilen("Juni", (("blue Mobile M", "55.00"),)))
    eine = _buchung(date(2031, 6, 20), "-54.95", "Beispielfunk mehrdeutig A")
    andere = _buchung(date(2031, 7, 10), "-54.95", "Beispielfunk mehrdeutig B")

    assert _abgleich(tmp_path) == 0
    assert _anhang(eine) is None
    assert _anhang(andere) is None


def test_belegte_buchung_bekommt_nicht_noch_eine_rechnung(tmp_path: Path) -> None:
    """An der Buchung hängt schon ein Beleg — dann ist sie vergeben.

    Ohne diese Bedingung landete die Rechnung des einen Monats an der Buchung,
    an der schon die des Vormonats hängt, und der Vormonat verlöre seinen Beleg
    nicht einmal sichtbar: es stünden zwei Belege an derselben Zahlung.
    """
    _pdf(tmp_path / "beispielfunk_august.pdf", _zeilen("August", (("blue Mobile M", "77.00"),)))
    tx_id = _buchung(date(2031, 8, 25), "-76.95", "Beispielfunk schon belegt")
    with SessionLocal() as db:
        db.add(Attachment(transaction_id=tx_id, original_name="anderer_beleg.pdf"))
        db.commit()

    assert _abgleich(tmp_path) == 0
    with SessionLocal() as db:
        anhaenge = db.scalars(select(Attachment).where(Attachment.transaction_id == tx_id)).all()
    assert [a.original_name for a in anhaenge] == ["anderer_beleg.pdf"]


def test_von_hand_zugeordnete_rechnung_traegt_dieselben_positionen(tmp_path: Path) -> None:
    """Der Fall, den es garantiert gibt: bei zwei passenden Buchungen ordnet die
    Automatik ausdrücklich NICHT zu — der Nutzer tut es.

    Hinge die Aufstellung daran, dass die Zuordnung automatisch entstand, fehlte
    sie ausgerechnet dort, wo jemand hingeschaut hat.
    """
    _pdf(tmp_path / "beispielfunk_september.pdf", _zeilen("September"))
    tx_id = _buchung(date(2031, 10, 20), "-69.95", "Beispielfunk von Hand")

    alt = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            attach_receipt(db, db.get(Transaction, tx_id), "beispielfunk_september.pdf")
            db.commit()
    finally:
        settings.receipts_dir = alt

    daten = json.loads(_anhang(tx_id).parsed_items_json)
    assert daten["method"] == "rechnung"
    assert len(daten["items"]) == 3


def test_text_neu_auslesen_behaelt_die_positionen(tmp_path: Path) -> None:
    """Der Knopf schrieb die geprüften Positionen sonst mit einem geschätzten
    Betrag zu — ein Klick, und die Aufstellung an der Buchung wäre weg."""
    _pdf(tmp_path / "beispielfunk_oktober.pdf", _zeilen("Oktober"))
    tx_id = _buchung(date(2031, 11, 20), "-69.95", "Beispielfunk neu auslesen")

    alt = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            att = attach_receipt(db, db.get(Transaction, tx_id), "beispielfunk_oktober.pdf")
            db.commit()
            att_id = att.id
        with SessionLocal() as db:
            att = db.get(Attachment, att_id)
            read_receipt_data(att, att.file_path)
            db.commit()
    finally:
        settings.receipts_dir = alt

    daten = json.loads(_anhang(tx_id).parsed_items_json)
    assert daten["method"] == "rechnung"
    assert len(daten["items"]) == 3


# ----------------------------------------------------- Verbindungsnachweis


def _nachweis_pdf(ziel: Path) -> Path:
    """Ein erfundener Verbindungsnachweis: Nutzung, keine Zahlung.

    Sein Text trägt trotzdem eine Summe — genau daran hinge die allgemeine
    Zuordnung, die Beträge aus dem Text schätzt.
    """
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text(
        (72, 72),
        "Beispielfunk Verbindungsnachweis\n15.09.2031\nTelefonie Inland 0.00\nTotal CHF 88.15",
    )
    doc.save(str(ziel))
    doc.close()
    return ziel


def test_verbindungsnachweis_wird_keiner_buchung_zugeordnet(tmp_path: Path) -> None:
    """Er weist Nutzung aus, keine Zahlung.

    Die Buchung hier passt nach Betrag UND Datum — über die allgemeine
    Zuordnung wäre er damit ein sicherer Treffer. Er gehört trotzdem an keine
    Buchung: was er belegt, ist kein Geldfluss.
    """
    _nachweis_pdf(tmp_path / "nachweis.pdf")
    tx_id = _buchung(date(2031, 9, 15), "-88.15", "Beispielfunk Zahlung September")

    assert _abgleich(tmp_path) == 0
    assert _anhang(tx_id) is None


def test_verbindungsnachweis_bekommt_auch_keinen_vorschlag(tmp_path: Path) -> None:
    """Der Assistent legt den besten Kandidaten vor, und der Nutzer bestätigt ihn
    mit einem Klick. Für einen Beleg, der an keine Buchung gehört, wäre jeder
    Vorschlag ein Fehltritt, den er nur noch abnicken muss."""
    _nachweis_pdf(tmp_path / "nachweis_vorschlag.pdf")
    _buchung(date(2031, 10, 15), "-88.15", "Beispielfunk Zahlung Oktober")

    alt = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            vorschlaege = build_suggestions(db, ocr=False)
    finally:
        settings.receipts_dir = alt

    assert len(vorschlaege) == 1
    assert vorschlaege[0].best is None
    assert vorschlaege[0].reason == KEIN_ZAHLUNGSBELEG


# ------------------------------------------------------ Anzeige an der Buchung


def _anhang_mit(daten: dict, *, tag: date, betrag: str, text: str) -> int:
    """Buchung mit einem strukturierten Anhang — ohne Datei, wie beim Foto-Beleg."""
    tx_id = _buchung(tag, betrag, text)
    with SessionLocal() as db:
        db.add(Attachment(transaction_id=tx_id, original_name="beleg.pdf",
                          parsed_items_json=json.dumps(daten, ensure_ascii=False)))
        db.commit()
    return tx_id


def test_nur_geprueft_gelesene_positionen_werden_aufgestellt() -> None:
    """Die Positionen eines Kassenbons sind OCR-geraten und ergeben zusammen
    nicht den gebuchten Betrag. Untereinander gestellt sähen sie genauso
    verbindlich aus wie die einer gelesenen Rechnung."""
    bon = {"method": "ocr", "merchant": "Irgendein Laden",
           "items": [{"name": "Brot", "price": "4.20"}]}
    assert rechnungsbeleg.anzeige_posten(bon) == []


def test_die_rundungsdifferenz_steht_in_der_aufstellung() -> None:
    """Ohne sie ergäben die Zeilen den gebuchten Betrag nicht — und wer
    nachrechnet, fände einen Fehler, wo keiner ist."""
    befund = rechnungsbeleg.rechnung_aus_text(_spaltentext(_zeilen("März")))
    posten = rechnungsbeleg.anzeige_posten(rechnungsbeleg.strukturiert(befund))
    assert posten[-1].name == "Rundungsdifferenz"
    assert sum((p.betrag for p in posten), Decimal("0")) == befund.value


def test_buchungszeile_klappt_die_positionen_auf(logged_in_client: TestClient) -> None:
    """Die Aufstellung hängt an der Buchung — zugeklappt, und nur dort, wo eine
    gelesene Rechnung hängt. An jeder Zeile wäre sie auf 375px reiner Lärm."""
    daten = {"method": "rechnung", "merchant": "Beispielfunk", "amount": "69.95",
             "items": [{"name": "blue Mobile M", "price": "80.00"},
                       {"name": "Kombi-Rabatt", "price": "-20.00"}]}
    # Im laufenden Jahr: die Liste zeigt vorgabegemäss dieses Jahr, und geprüft
    # wird hier die Zeile, nicht der Zeitraumfilter.
    _anhang_mit(daten, tag=date.today(), betrag="-69.95", text="ZZBELEGMIT Beispielfunk")
    _anhang_mit({"method": "ocr", "items": [{"name": "Brot", "price": "4.20"}]},
                tag=date.today(), betrag="-4.20", text="ZZBELEGOHNE Laden")

    antwort = logged_in_client.get("/transactions")
    assert antwort.status_code == 200
    # Je Zeile prüfen: andere Tests hinterlassen eigene Belege in derselben DB.
    zeilen = antwort.text.split('<div class="tx-row')
    mit = next(z for z in zeilen if "ZZBELEGMIT" in z)
    ohne = next(z for z in zeilen if "ZZBELEGOHNE" in z)

    assert mit.startswith(" hat-posten"), "die Zeile braucht die zweite Gitterzeile"
    assert "bposten-kopf" in mit
    assert "blue Mobile M" in mit
    assert "−20.00" in mit, "der Rabatt steht mit seinem Vorzeichen da"
    assert "bposten-kopf" not in ohne, "ohne gelesene Rechnung kein Aufklapper"


def test_aufklapper_ist_ein_tippziel() -> None:
    """Der 44px-Wächter zählte Bedienelemente als KLASSEN auf, und ein
    ``<summary>`` stand dort nicht von allein drin — der Lohn-Aufklapper mass
    deshalb 28px, bis er eingetragen wurde. Dieser hier ist dasselbe Element.

    Die Aufzählung ist inzwischen weg: die Regel greift an ``details > summary``
    und damit an der Bauform. Geprüft wird weiterhin dieselbe Zusage, nur nicht
    mehr über den Klassennamen — sonst wäre dieser Test rot, sobald jemand die
    Liste zugunsten der allgemeinen Regel aufräumt, und grün, während ein
    anderer Aufklapper durchfällt."""
    import re

    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")
    block = css[css.index("Touch-Ziele"):]
    block = block[:block.index("min-height: 44px;")]
    # Kommentare RAUS, bevor gesucht wird: im Kommentar der Regel steht der
    # Klassenname als Begründung, und der Test bliebe grün, wenn nur der
    # Selektor entfernt würde.
    selektoren = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    assert "details:not(.rowmenu) > summary" in selektoren, (
        "Der Aufklapper der Rechnungspositionen ist ein <summary> und bekommt "
        "sein Tippziel ueber die Bauform. Fehlt diese Regel, faellt er zurueck.")
    # Und er ist wirklich ein <summary> — sonst prueft die Regel oben nichts.
    markup = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
              / "partials" / "beleg_posten.html")
    assert '<summary class="bposten-kopf"' in markup.read_text(encoding="utf-8"), (
        "Der Positionsblock hat keinen <summary>-Aufklapper mehr.")
