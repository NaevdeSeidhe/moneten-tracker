"""Die Befunde der Gegenprüfung  — je Befund ein Test.

Diese Datei hält fest, was schon einmal falsch war. Jeder Test hier stellt
einen gemessenen Fehler her, nicht eine denkbare Gefahr; die Reihenfolge folgt
der Schwere, mit der die Befunde ankamen.

**Alle Beträge, Namen und Texte sind erfunden.** Nichts hier stammt aus einem
Beleg (siehe ``test_keine_echten_daten``). Die Schreibweisen der Summenzeilen
sind Kassen-Standardtexte, keine abgeschriebenen Belege — die Zahlen daneben
sind gesetzt.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from moneten.db.models import Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services import rechnungsbeleg
from moneten.services.belege_parser import Befund
from moneten.services.price_history import ist_artikel
from moneten.services.receipt_digital import (
    _GENERISCH_AB,
    RUNDUNGS_TOLERANZ,
    _item_keyword,
    _nur_scheinbar_geprueft,
    pruefe_positionen,
)
from moneten.services.receipt_split import parse_receipt_items_menge
from moneten.services.rechnungsbeleg import ist_verbindungsnachweis as ist_verbindungsnachweis_hilfe

# ---------------------------------------------------------------------------
# Beleg-Scan
# ---------------------------------------------------------------------------


def _posten(*paare: tuple[str, str]) -> list[dict]:
    return [{"name": n, "price": p} for n, p in paare]


@pytest.mark.parametrize("preis", ["NaN", "nan", "Infinity", "-Infinity", "sNaN"])
def test_unzahl_im_preisfeld_stuerzt_nicht_ab(preis: str):
    """``Decimal("NaN")`` entsteht ohne Ausnahme — und riss sie erst beim Vergleich.

    Der Wert kommt ungeprüft aus dem Browser. ``Decimal(str(raw))`` schluckt
    „NaN" klaglos, ``quantize`` ebenso; erst ``abs(summe - total)`` warf eine
    ``InvalidOperation``. Das war ein 500er mitten im Speichern eines Belegs,
    also genau in dem Moment, in dem die Arbeit verloren geht.
    """
    _, probe = pruefe_positionen(_posten(("Ware", preis), ("Ware B", "45.60")),
                                 Decimal("45.60"))
    assert probe.ok is False


def test_zwei_fehler_die_sich_aufheben_bestehen_die_probe_nicht():
    """Ein reiner Summentest lässt sich mit zwei Fehlern austricksen.

    −20.00 und +65.60 ergeben zusammen den Total von 45.60. Vorher galt die
    Aufstellung damit als geprüft, und der negative „Preis" wanderte in
    ``parsed_items_json`` und in den Preisverlauf. Ein Kassenbon hat keine
    negative Position: ein Rabatt steckt im Zeilen-Total, eine Rückgabe ist ein
    eigener Beleg.
    """
    _, probe = pruefe_positionen(_posten(("Ware", "-20.00"), ("Ware B", "65.60")),
                                 Decimal("45.60"))
    assert probe.ok is False


def test_gueltige_aufstellung_besteht_weiter():
    """Die Gegenprobe darf nicht durch Strenge nutzlos werden."""
    _, probe = pruefe_positionen(_posten(("Ware", "20.00"), ("Ware B", "25.60")),
                                 Decimal("45.60"))
    assert probe.ok is True


@pytest.mark.parametrize("zeile", [
    "Total bezahlt (Karte): 20.00 CHF",
    "Zu bezahlen 20.00",
    "Endbetrag 20.00",
    "Gesamt 20.00",
    "Gesamtbetrag 20.00",
    "Zahlbetrag 20.00",
    "Rechnungsbetrag 20.00",
    "Bezahlt 20.00",
])
def test_summenzeilen_werden_nicht_zur_position(zeile: str):
    """Diese Schreibungen fehlten in ``_SKIP`` — gemessen an einem Kinobeleg.

    Der Beleg gab GENAU eine Zeile her, und zwar die Totalzeile. Die Gegenprobe
    ging damit auf (Summe = Total ist da eine Identität), der Beleg galt als
    geprüft, wurde gespeichert UND gelernt.
    """
    assert parse_receipt_items_menge(zeile) == []


def test_echte_einzelposition_bleibt_erhalten():
    """Die Sperre darf keinen Beleg mit nur einem Artikel auslöschen."""
    assert len(parse_receipt_items_menge("Kinoticket 20.00")) == 1


def test_aus_einer_position_gleich_total_wird_nicht_gelernt():
    """Wo die Probe eine Identität ist, darf sie keine Regel begründen.

    Gespeichert wird die Position weiter — dort ist sie sichtbar und lässt sich
    richtigstellen. Eine gelernte Regel dagegen wirkt still auf jeden künftigen
    Beleg jedes Händlers.
    """
    quittung = {"amount": "20.00",
                "items": [{"name": "Kinoticket", "price": "20.00", "category_id": 3}]}
    assert _nur_scheinbar_geprueft(quittung) is True


def test_aus_zwei_positionen_wird_gelernt():
    """Ab zwei Positionen hat die Summe etwas geprüft."""
    quittung = {"amount": "20.00", "items": [
        {"name": "Ticket", "price": "12.00", "category_id": 3},
        {"name": "Getränk", "price": "8.00", "category_id": 3},
    ]}
    assert _nur_scheinbar_geprueft(quittung) is False


def test_toleranz_bleibt_die_eine_zahl():
    """Die Gegenprobe und die Lern-Sperre messen mit demselben Mass."""
    fast = str(Decimal("20.00") + RUNDUNGS_TOLERANZ)
    quittung = {"amount": "20.00",
                "items": [{"name": "Ticket", "price": fast, "category_id": 3}]}
    assert _nur_scheinbar_geprueft(quittung) is True


# ---------------------------------------------------------------------------
# Zuordnung der Anbieter-Rechnung
# ---------------------------------------------------------------------------


def _befund(wert: str = "111.10") -> Befund:
    return Befund(
        slug="beispielfunk",
        value=Decimal(wert),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        extras={},
    )


def _buchung(db, konto_id: int, tag: date, betrag: str, text: str) -> Transaction:
    tx = Transaction(account_id=konto_id, date=tag, amount=Decimal(betrag),
                     description=text)
    db.add(tx)
    db.flush()
    return tx


@pytest.fixture()
def sitzung():
    """Eine Sitzung, die hinter sich aufräumt.

    Die Testdatenbank ist über die ganze Datei hinweg dieselbe; was hier
    entsteht, darf keinen anderen Test sehen — :func:`passende_buchung` liest
    ALLE Buchungen des Fensters, nicht nur die eigenen.
    """
    from moneten.db.models import Account, AccountType
    from moneten.db.session import SessionLocal

    with SessionLocal() as db:
        konto = Account(name="Gegenprobe-Konto", type=AccountType.BANK, currency="CHF",
                        opening_balance=Decimal("0"), current_balance=Decimal("0"),
                        sort_order=901)
        db.add(konto)
        db.flush()
        try:
            yield db, konto.id
        finally:
            db.rollback()


def test_belegte_buchung_schafft_keine_eindeutigkeit(sitzung):
    """Der schwerste Befund: das Abziehen belegter Buchungen SCHUF Eindeutigkeit.

    Nachgestellt ist der gemessene Fall: zwei betragsgleiche Ausgaben im
    Fenster, die richtige trägt schon einen Anhang (einen Kassenbon, nicht die
    Rechnung des Vormonats). Vorher fiel sie damit vor der Eindeutigkeitsprüfung
    heraus, und die Rechnung landete an der anderen.
    """
    db, konto_id = sitzung
    richtig = _buchung(db, konto_id, date(2026, 1, 20), "-111.10", "BEISPIELFUNK AG")
    _buchung(db, konto_id, date(2026, 2, 10), "-111.10", "Beispielfunk Rechnung")
    db.add(Attachment(transaction_id=richtig.id, file_path="bon.jpg",
                      original_name="bon.jpg"))
    db.flush()

    assert rechnungsbeleg.passende_buchung(db, _befund()) is None


def test_kette_darf_sich_monat_um_monat_fuellen(sitzung):
    """Hängt am Vormonat schon die RECHNUNG, ist die freie Buchung eindeutig.

    Das ist der Fall, für den das Abziehen gedacht war — und der einzige, in
    dem es etwas beweist.
    """
    db, konto_id = sitzung
    vormonat = _buchung(db, konto_id, date(2026, 1, 20), "-111.10", "Beispielfunk")
    frei = _buchung(db, konto_id, date(2026, 2, 10), "-111.10", "Beispielfunk")
    db.add(Attachment(
        transaction_id=vormonat.id, file_path=None, original_name="Beispielfunk",
        parsed_items_json=json.dumps(
            {"method": rechnungsbeleg.METHODE, "merchant": "Beispielfunk"}),
    ))
    db.flush()

    assert rechnungsbeleg.passende_buchung(db, _befund()) is frei


def test_fremde_ausgabe_mit_gleichem_betrag_wird_nicht_genommen(sitzung):
    """Ohne den Händlernamen entschied allein der Betrag.

    Fehlte die richtige Buchung, griff die Rechnung nach der nächstbesten
    betragsgleichen Ausgabe im Fenster.
    """
    db, konto_id = sitzung
    _buchung(db, konto_id, date(2026, 1, 20), "-111.10", "Moebelhaus Rechnung")
    assert rechnungsbeleg.passende_buchung(db, _befund()) is None


def test_eine_eindeutige_zahlung_wird_zugeordnet(sitzung):
    """Der Normalfall muss weiter greifen."""
    db, konto_id = sitzung
    tx = _buchung(db, konto_id, date(2026, 1, 20), "-111.10", "BEISPIELFUNK AG")
    assert rechnungsbeleg.passende_buchung(db, _befund()) is tx


def test_verbindungsnachweis_wird_ueberall_im_text_gefunden():
    """Die Sperre sucht im GANZEN Dokument — bewusst, obwohl sie zu viel fängt.

    Der Versuch, sie auf den Titel einzugrenzen (erste Zeilen, kurze Zeile), war
    ein Rückschritt: nachgemessen fiel eine Titelzeile von 75 Zeichen durch, und
    ein durchgelassener Nutzungsbeleg landet über die geschätzte Zuordnung an
    der echten Anbieter-Zahlung. Danach ist sie belegt, und die richtige
    Rechnung findet kein freies Gegenstück mehr.

    Was die Sperre zu viel fängt, löst nicht sie, sondern die Reihenfolge beim
    Aufrufer — siehe :func:`test_rechnung_schlaegt_die_sperre`.
    """
    langer_titel = ("Beispielfunk\nVerbindungsnachweis für Ihre Mobilnummer im "
                    "Zeitraum August 2026, Seite 1 von 4\n")
    tief_im_text = "Beispielfunk\nRechnung\n" + "\n" * 40 + "Verbindungsnachweis\n"
    assert ist_verbindungsnachweis_hilfe(langer_titel) is True
    assert ist_verbindungsnachweis_hilfe(tief_im_text) is True


def test_rechnung_schlaegt_die_sperre(tmp_path, monkeypatch):
    """Was als Rechnung aufgeht, IST eine Rechnung — auch mit dem Wort im Text.

    Geprüft wird der Vorschlags-Weg, weil dort der Fehler stand: die Sperre lief
    vor dem Parser, und eine Rechnung, die den Begriff nur erwähnt, bekam „kein
    Zahlungsbeleg" samt Rat, sie wegzuräumen. Und zwar genau dann, wenn die
    Automatik sie NICHT zugeordnet hat — im einzigen Moment, in dem der
    Assistent zählt.

    Der Parser wird hier ersetzt: eine echte Anbieter-Rechnung nachzubauen
    hiesse, ihre Spaltengeometrie UND ihre Prüfsumme nachzubauen. Gemessen wird
    die Reihenfolge, und die ist unabhängig davon, wie der Parser zu seinem
    Befund kommt.
    """
    import fitz

    from moneten.config import settings
    from moneten.services import receipt_match

    pfad = tmp_path / "gp_2026-01-05_beispielfunk.pdf"
    doc = fitz.open()
    doc.new_page().insert_text(
        (72, 72), "Beispielfunk\nRechnung Januar 2026\nIhre Verbindungsnachweise im Portal.")
    doc.save(str(pfad))
    doc.close()

    monkeypatch.setattr(receipt_match.rechnungsbeleg, "rechnung_zur_datei",
                        lambda *_a, **_k: _befund())
    monkeypatch.setattr(receipt_match.rechnungsbeleg, "passende_buchung",
                        lambda *_a, **_k: None)
    alt_dir = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            vorschlaege = receipt_match.build_suggestions(db, ocr=False)
            assert len(vorschlaege) == 1
            assert vorschlaege[0].reason != receipt_match.KEIN_ZAHLUNGSBELEG
    finally:
        settings.receipts_dir = alt_dir


# ---------------------------------------------------------------------------
# Zweite Runde der Gegenpruefung: was die Fixes selbst noch aufgedeckt haben
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "erwartet"), [
    ("Aus der Region Rüebli", "rüebli"),
    ("M-Classic Waschmittel Color", "waschmittel"),
    ("Bio Kerze Citronella", "kerze"),
    ("Prix Garantie Milch 1l", "milch"),
    ("Zahnpasta Elmex", "zahnpasta"),
])
def test_label_wird_nicht_zum_lern_stichwort(name: str, erwartet: str):
    """Das Stichwort war das ERSTE Wort — und das ist oft das Label.

    Gemessen: ein Bon mit diesen Zeilen lernte „aus", „classic", „bio", „prix".
    Weil eine Regel auch generisch (ohne Händler) angelegt wird, schlug sie
    danach bei jedem Händler zu und VOR dem eingebauten Lexikon — „Bio Kerze
    Citronella" landete unter Lebensmitteln statt unter Haushalt.
    """
    assert _item_keyword(name) == erwartet


def test_kurzes_stichwort_bleibt_beim_haendler():
    """Bleibt nach der Sperrliste nur ein Rest, darf er nicht generisch werden.

    „Bio" allein ergibt kein anderes Stichwort — dann ist die Händler-Regel in
    Ordnung, die generische nicht: sie wäre das scharfe Werkzeug ohne den
    Kontext, der sie rechtfertigt.
    """
    assert _item_keyword("Bio") == "bio"
    assert len("bio") < _GENERISCH_AB


@pytest.mark.parametrize("name", [
    "Endbetrag", "Gesamt", "Gesamtbetrag", "Zahlbetrag", "Rechnungsbetrag",
    "Bezahlt", "Zu bezahlen", "Summe",
])
def test_summenzeile_kommt_nicht_in_den_preisverlauf(name: str):
    """Das ZWEITE Tor stand offen, während das erste geschlossen wurde.

    ``receipt_split._SKIP`` hält diese Wortlaute ab jetzt vom Beleg fern. Im
    Bestand liegen aber genau die Positionen, die vorher durchgingen — und ein
    „Artikel", dessen Preis das Belegtotal ist, sieht im Verlauf wie Teuerung
    aus. ``price_history.ist_artikel`` ist die Stelle, die den Bestand noch
    filtern kann.
    """
    assert ist_artikel(name) is False


def test_echter_artikel_bleibt_im_preisverlauf():
    """Die Erweiterung darf keine Ware ausschliessen."""
    for name in ("Bio Butter", "Zahnpasta Elmex", "Kinoticket"):
        assert ist_artikel(name) is True
