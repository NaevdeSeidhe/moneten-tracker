"""Lohnzusammensetzung an einer Gutschrift: Bruttolohn, Abzüge, Nettolohn.

Der Kern dieser Tests ist nicht das Rechnen, sondern die **Ehrlichkeit** der
Zahlen. Es gibt keine monatlichen Lohnabrechnungen; ein Monat lässt sich nur aus
Jahreswerten schätzen. Drei Zusicherungen hängen daran, und jede hat hier ihren
eigenen Test:

* Der Nettolohn wird aus den Posten gerechnet und NIE an den gebuchten Betrag
  angeglichen — sonst sähe eine Schätzung exakt aus.
* Was hergeleitet ist, bleibt als hergeleitet gespeichert, bis der Nutzer die
  Zahl ändert.
* Die Abweichung zum gebuchten Betrag wird gezeigt und nicht weggerechnet.

Alle Beträge sind erfunden.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    Category,
    Lohnabrechnung,
    LohnHerkunft,
    Lohnposten,
    LohnPostenArt,
    ManagementType,
    MetricPoint,
    MetricSeries,
    Transaction,
    TransactionSplit,
)
from moneten.db.session import SessionLocal
from moneten.services import lohn as lohn_service

# Ein Konto für die ganze Datei. Bewusst gemerkt statt je Aufruf neu angelegt:
# eine zweite Session innerhalb einer offenen Schreib-Session sperrt die
# SQLite-Datei („database is locked").
_KONTO: list[int] = []


def _konto() -> int:
    if not _KONTO:
        with SessionLocal() as db:
            acc = Account(name="Lohn-Konto", type=AccountType.BANK, currency="CHF",
                          opening_balance=Decimal("0"), current_balance=Decimal("0"),
                          sort_order=900)
            db.add(acc)
            db.commit()
            _KONTO.append(acc.id)
    return _KONTO[0]


def _gutschrift(betrag: str, beschreibung: str, *, tag: date | None = None) -> int:
    """Eine Einnahme-Buchung — Nettolohn ist positiv."""
    konto = _konto()
    with SessionLocal() as db:
        tx = Transaction(account_id=konto, category_id=None, date=tag or date.today(),
                         amount=Decimal(betrag), description=beschreibung)
        db.add(tx)
        db.commit()
        return tx.id


def _mit_posten(tx_id: int, posten: list[tuple[LohnPostenArt, str, str, LohnHerkunft]],
                *, grundlage: str = "Testfall") -> None:
    with SessionLocal() as db:
        a = Lohnabrechnung(transaction_id=tx_id, grundlage=grundlage)
        db.add(a)
        db.flush()
        for i, (art, label, betrag, herkunft) in enumerate(posten):
            db.add(Lohnposten(abrechnung_id=a.id, art=art, label=label,
                              betrag=Decimal(betrag), herkunft=herkunft, sort_order=i))
        db.commit()


def _alle_abrechnungen_weg() -> None:
    """Vorschläge speisen sich aus früheren Aufstellungen — für die Tests der
    ABGELEITETEN Quellen muss der Bestand darum leer sein."""
    with SessionLocal() as db:
        for a in db.scalars(select(Lohnabrechnung)):
            db.delete(a)
        db.commit()


# ---------------------------------------------------------------------------
# Ehrlichkeit der Zahlen
# ---------------------------------------------------------------------------


def test_nettolohn_wird_gerechnet_und_nicht_an_die_buchung_angeglichen() -> None:
    """Der Nettolohn kommt aus den Posten, nicht aus dem gebuchten Betrag.

    Der teuerste denkbare Fehler dieses Moduls: die Aufstellung an die Buchung
    anzugleichen. Sie sähe dann in jedem Monat exakt aus — auch dort, wo jeder
    Posten aus einem Jahreswert geschätzt ist. Die Abweichung ist die einzige
    Angabe, an der sich die Güte der Schätzung überhaupt ablesen lässt.
    """
    tx_id = _gutschrift("4150.00", "Lohn Mai Testfirma")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "500.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "Pensionskasse", "300.00", LohnHerkunft.ERFASST),
    ])
    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        a = lohn_service.aufstellung(lohn_service.abrechnung_zu(db, tx_id), tx)

    assert a.brutto == Decimal("5000.00")
    assert a.abzuege == Decimal("800.00")
    assert a.netto == Decimal("4200.00"), "Nettolohn = Brutto − Abzüge, nicht der gebuchte Betrag"
    assert a.gebucht == Decimal("4150.00")
    assert a.differenz == Decimal("50.00")


def test_differenz_ist_nur_bei_lauter_erfassten_werten_ein_fehler() -> None:
    """Eine Abweichung bedeutet zweierlei — je nachdem, was die Posten behaupten.

    Steckt eine Schätzung drin, ist die Differenz der Normalfall. Sind alle
    Beträge erfasst, behauptet die Aufstellung, abgelesen zu sein; dann ist eine
    Abweichung ein Tippfehler oder ein vergessener Abzug.
    """
    geschaetzt_id = _gutschrift("4150.00", "Lohn Juni geschaetzt")
    _mit_posten(geschaetzt_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "800.00", LohnHerkunft.ERFASST),
    ])
    erfasst_id = _gutschrift("4150.00", "Lohn Juni erfasst")
    _mit_posten(erfasst_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "800.00", LohnHerkunft.ERFASST),
    ])

    with SessionLocal() as db:
        geschaetzt = lohn_service.aufstellung(
            lohn_service.abrechnung_zu(db, geschaetzt_id), db.get(Transaction, geschaetzt_id))
        erfasst = lohn_service.aufstellung(
            lohn_service.abrechnung_zu(db, erfasst_id), db.get(Transaction, erfasst_id))

    assert geschaetzt.differenz == erfasst.differenz == Decimal("50.00")
    assert geschaetzt.geschaetzt is True
    assert geschaetzt.differenz_ist_fehler is False
    assert erfasst.differenz_ist_fehler is True


def test_unveraenderter_vorschlag_bleibt_gerechnet() -> None:
    """Wer eine Zahl ändert, verantwortet sie — wer sie stehen lässt, nicht.

    Ohne diese Unterscheidung wäre jeder gespeicherte Posten „erfasst", und die
    Kennzeichnung im Aufklapper behauptete Genauigkeit, die es nicht gibt.
    """
    # Erkennbar erfundene Zahlen: geprüft wird „gleich oder nicht", dafür taugt
    # jede. Ein Betrag, der wie ein echter Abzug aussieht, hätte hier nichts
    # verloren — auch nicht als Beispiel in einem Kommentar.
    unveraendert = lohn_service.herkunft_nach_aenderung(
        Decimal("111.11"), Decimal("111.11"), "gerechnet")
    veraendert = lohn_service.herkunft_nach_aenderung(
        Decimal("222.22"), Decimal("111.11"), "gerechnet")
    neu_getippt = lohn_service.herkunft_nach_aenderung(Decimal("42.00"), None, "")

    assert unveraendert == LohnHerkunft.GERECHNET
    assert veraendert == LohnHerkunft.ERFASST
    assert neu_getippt == LohnHerkunft.ERFASST


def test_vorschlag_aus_dem_jahreslohn_ist_als_gerechnet_gekennzeichnet() -> None:
    """Aus einem Jahreswert wird ein Monat nur geschätzt — und so gekennzeichnet.

    Zugleich die Zusicherung, dass für NBUV und Pensionskasse ohne Quelle nichts
    erfunden wird: die Zeilen stehen leer da. Eine sichtbare Lücke ist besser
    als eine unsichtbare Erfindung.
    """
    _alle_abrechnungen_weg()
    tx_id = _gutschrift("4150.00", "Lohn aus Jahreswert", tag=date(2019, 5, 25))
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "lohn"))
        punkt = MetricPoint(series_id=reihe.id, period_start=date(2019, 1, 1),
                            period_end=date(2019, 12, 31), value=Decimal("60000.00"))
        db.add(punkt)
        db.commit()
        try:
            zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, tx_id))
        finally:
            db.delete(punkt)
            db.commit()

    nach_label = {z["label"]: z for z in zeilen}
    assert nach_label["Bruttolohn"]["betrag"] == "5000.00"  # 60'000 ÷ 12
    assert grundlage == "Jahreslohn 2019 ÷ 12"
    assert all(z["herkunft"] == "gerechnet" for z in zeilen)
    assert nach_label["AHV/IV/EO"]["betrag"] == "265.00"  # 5.3 % des Monatsbruttos
    assert nach_label["NBUV"]["betrag"] == "", "ohne Quelle darf kein Wert erfunden werden"


# ---------------------------------------------------------------------------
# Erfassen über die Oberfläche
# ---------------------------------------------------------------------------


def test_speichern_haelt_die_herkunft_je_posten_fest(logged_in_client: TestClient) -> None:
    """Der Weg durch die Route: unveränderte Zeile bleibt gerechnet, geänderte wird erfasst."""
    tx_id = _gutschrift("4150.00", "Lohn Route Herkunft")
    r = logged_in_client.post(f"/transactions/{tx_id}/lohn", data={
        "lohn_art": ["brutto", "abzug"],
        "lohn_label": ["Bruttolohn", "AHV/IV/EO"],
        "lohn_betrag": ["5000.00", "312.50"],
        "lohn_alt": ["5000.00", "265.00"],
        "lohn_herkunft": ["gerechnet", "gerechnet"],
        "lohn_grundlage": "Lohnausweis 2019 ÷ 12",
    })
    assert r.status_code == 200

    with SessionLocal() as db:
        a = lohn_service.abrechnung_zu(db, tx_id)
        nach_label = {p.label: p for p in a.posten}
        assert a.grundlage == "Lohnausweis 2019 ÷ 12"
        assert nach_label["Bruttolohn"].herkunft == LohnHerkunft.GERECHNET
        assert nach_label["AHV/IV/EO"].herkunft == LohnHerkunft.ERFASST
        assert nach_label["AHV/IV/EO"].betrag == Decimal("312.50")


def test_speichern_ruehrt_betrag_und_aufteilung_der_buchung_nicht_an(
    logged_in_client: TestClient,
) -> None:
    """Die Aufstellung erklärt eine Buchung — sie verändert sie nicht.

    Beides zusammen geprüft, weil beides derselbe Fehler wäre: der gebuchte
    Betrag ist die Tatsache, gegen die die Aufstellung geprüft wird. Passte sich
    die Buchung an, wäre die Gegenprobe wertlos. Und die Kategorie-Aufteilung
    (TransactionSplit) verteilt denselben Betrag auf Kategorien — sie hat mit
    der Lohn-Herleitung nichts zu tun und darf davon nicht angefasst werden.
    """
    konto = _konto()
    with SessionLocal() as db:
        kat_a = Category(name="LohnTest-A", management_type=ManagementType.EINKOMMEN)
        kat_b = Category(name="LohnTest-B", management_type=ManagementType.EINKOMMEN)
        db.add_all([kat_a, kat_b])
        db.flush()
        tx = Transaction(account_id=konto, category_id=None, date=date.today(),
                         amount=Decimal("4150.00"), description="Lohn aufgeteilt", is_split=True)
        db.add(tx)
        db.flush()
        db.add(TransactionSplit(transaction_id=tx.id, category_id=kat_a.id, amount=Decimal("3000.00")))
        db.add(TransactionSplit(transaction_id=tx.id, category_id=kat_b.id, amount=Decimal("1150.00")))
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(f"/transactions/{tx_id}/lohn", data={
        "lohn_art": ["brutto", "abzug"],
        "lohn_label": ["Bruttolohn", "Pensionskasse"],
        "lohn_betrag": ["5000.00", "800.00"],
        "lohn_alt": ["", ""],
        "lohn_herkunft": ["", ""],
        "lohn_grundlage": "",
    })
    assert r.status_code == 200

    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        assert tx.amount == Decimal("4150.00"), "Der gebuchte Betrag bleibt unangetastet"
        assert tx.is_split is True
        anteile = db.scalars(
            select(TransactionSplit).where(TransactionSplit.transaction_id == tx_id)).all()
        assert sum((s.amount for s in anteile), Decimal("0")) == Decimal("4150.00")
        # Verglichen wird gegen den Eltern-Betrag, nicht gegen die Anteile.
        a = lohn_service.aufstellung(lohn_service.abrechnung_zu(db, tx_id), tx)
        assert a.gebucht == Decimal("4150.00")
        assert a.netto == Decimal("4200.00")


def test_ausgaben_lassen_sich_nicht_als_lohn_aufschluesseln(
    logged_in_client: TestClient,
) -> None:
    """Nettolohn ist eine Einnahme. Eine Ausgabe hat keinen Bruttolohn."""
    konto = _konto()
    with SessionLocal() as db:
        tx = Transaction(account_id=konto, category_id=None, date=date.today(),
                         amount=Decimal("-89.50"), description="Keine Einnahme")
        db.add(tx)
        db.commit()
        tx_id = tx.id

    r = logged_in_client.post(f"/transactions/{tx_id}/lohn", data={
        "lohn_art": ["brutto"], "lohn_label": ["Bruttolohn"],
        "lohn_betrag": ["100.00"], "lohn_alt": [""], "lohn_herkunft": [""],
    })
    assert r.status_code == 400
    with SessionLocal() as db:
        assert lohn_service.abrechnung_zu(db, tx_id) is None


def test_editor_steht_nur_bei_einnahmen_im_formular(logged_in_client: TestClient) -> None:
    """Gegenprobe zur Route: der Editor wird gar nicht erst angeboten."""
    einnahme = _gutschrift("4150.00", "Lohn mit Editor")
    konto = _konto()
    with SessionLocal() as db:
        aus = Transaction(account_id=konto, category_id=None, date=date.today(),
                          amount=Decimal("-42.00"), description="Ausgabe ohne Editor")
        db.add(aus)
        db.commit()
        aus_id = aus.id

    mit = logged_in_client.get(f"/transactions?form=edit&id={einnahme}")
    ohne = logged_in_client.get(f"/transactions?form=edit&id={aus_id}")
    assert "lohn-editor" in mit.text
    assert "lohn-editor" not in ohne.text


# ---------------------------------------------------------------------------
# Anzeige an der Buchungszeile
# ---------------------------------------------------------------------------


def test_buchungszeile_klappt_die_aufstellung_auf(logged_in_client: TestClient) -> None:
    """Die Aufstellung hängt an der Buchung — und nur an der, die eine hat.

    Stünde der Aufklapper an jeder Zeile, wäre er auf 375px reiner Lärm; fehlte
    er an der Lohnzeile, führe die Erfassung ins Leere.
    """
    mit_id = _gutschrift("4150.00", "ZZLOHNMIT Gutschrift")
    _gutschrift("4150.00", "ZZLOHNOHNE Gutschrift")
    _mit_posten(mit_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.GERECHNET),
    ])

    r = logged_in_client.get("/transactions")
    assert r.status_code == 200
    # Je Buchungszeile prüfen statt über die ganze Seite: andere Tests hinterlassen
    # eigene Aufstellungen in derselben Test-Datenbank.
    zeilen = r.text.split('<div class="tx-row')
    mit = next(z for z in zeilen if "ZZLOHNMIT" in z)
    ohne = next(z for z in zeilen if "ZZLOHNOHNE" in z)

    assert mit.startswith(" hat-lohn"), "die Zeile braucht die zweite Gitterzeile"
    assert "lohn-kopf" in mit
    assert "Zusammensetzung" in mit
    # Die Kennzeichnung der gerechneten Werte darf nicht fehlen — ohne sie sieht
    # eine Schätzung aus wie eine Ablesung.
    assert "gerechnet, nicht abgelesen" in mit
    assert "lohn-kopf" not in ohne, "ohne Aufstellung kein Aufklapper"


def test_buchung_loeschen_nimmt_die_aufstellung_mit() -> None:
    """Keine Aufstellung ohne ihre Buchung — sonst bliebe eine Zahlenreihe
    zurück, die zu nichts mehr gehört."""
    tx_id = _gutschrift("4150.00", "Lohn wird geloescht")
    _mit_posten(tx_id, [(LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.ERFASST)])
    with SessionLocal() as db:
        a_id = lohn_service.abrechnung_zu(db, tx_id).id
        db.delete(db.get(Transaction, tx_id))
        db.commit()
        assert db.get(Lohnabrechnung, a_id) is None
        assert db.scalars(
            select(Lohnposten).where(Lohnposten.abrechnung_id == a_id)).all() == []


# ---------------------------------------------------------------------------
# Befunde der Gegenprobe — jeder Fix bekommt seine Absicherung
# ---------------------------------------------------------------------------


def test_nullbuchung_bekommt_keinen_lohn_editor() -> None:
    """Eine Buchung über 0.00 ist keine Lohnzahlung.

    Die Grenze lag bei ``>= 0`` und bot den Editor deshalb auch dort an.
    """
    from moneten.db.models import Transaction as Tx

    assert not lohn_service.darf_aufschluesseln(
        Tx(amount=Decimal("0.00"), management_type=None))
    assert lohn_service.darf_aufschluesseln(
        Tx(amount=Decimal("0.01"), management_type=None))


def test_rappen_werden_kaufmaennisch_gerundet() -> None:
    """Wie überall sonst im Projekt: ROUND_HALF_UP, nicht ROUND_HALF_EVEN.

    Die Decimal-Vorgabe rundet den halben Rappen zur GERADEN Ziffer: 1.125
    würde 1.12, 1.135 aber 1.14. Abos, Median-Budget und Jahresposten runden
    beide auf. Als einziger Baustein abzuweichen ergibt Zahlen, die sich um
    einen Rappen widersprechen.

    Erfundene Zahlen ohne Bezug zu einem Lohn — ein Beispiel, das wie ein
    echter Abzug aussieht, gehört nicht in einen Test.
    """
    assert lohn_service._runden(Decimal("1.125")) == Decimal("1.13")
    assert lohn_service._runden(Decimal("1.135")) == Decimal("1.14")


def _lohnblock(seite: str) -> str:
    """Das aufgeklappte BLATT einer Buchung — ohne den zugeklappten Kopf.

    Der Kopf trägt den Bruttolohn absichtlich ein zweites Mal (er soll auch
    zugeklappt eine Zahl zeigen). Wer auf Wiederholungen prüft, muss ihn deshalb
    aussen vor lassen, sonst zählt er die gewollte Dopplung mit.
    """
    assert 'class="lohn-blatt"' in seite, "Die Buchung zeigt keinen Lohnblock"
    block = seite[seite.index('class="lohn-blatt"'):]
    return block[:block.index("</details>")]


def test_einzelner_posten_behaelt_seine_bezeichnung(logged_in_client: TestClient) -> None:
    """Bei genau einem Brutto-Posten trug die Summenzeile „Bruttolohn" — und die
    Bezeichnung des Nutzers erschien nirgends.

    Sie ist aber die einzige Stelle, an der ein Pensum oder eine Besonderheit
    steht („Monatslohn brutto (80% Pensum)"). Geprüft wird am GERENDERTEN Block:
    im Dienst steht die Bezeichnung ohnehin, weggelassen hat sie das Template.
    """
    tx_id = _gutschrift("4150.00", "Einzelposten Brutto Kaeldra")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Monatslohn brutto (80% Pensum)", "5000.00",
         LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "ALV", "55.00", LohnHerkunft.ERFASST),
    ])
    with SessionLocal() as db:
        tx = db.get(Transaction, tx_id)
        a = lohn_service.aufstellung(lohn_service.abrechnung_zu(db, tx_id), tx)
    assert len(a.brutto_zeilen) == 1, "Vorbedingung: genau ein Brutto-Posten"

    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Einzelposten Brutto Kaeldra"}).text)
    assert "Monatslohn brutto (80% Pensum)" in block, (
        f"Die Bezeichnung des Nutzers fehlt im Block: {block!r}")
    assert block.count("5&#39;000.00") == 1, (
        f"Der einzige Brutto-Posten steht zweimal: {block!r}")


def test_einzelner_abzug_steht_nicht_zweimal(logged_in_client: TestClient) -> None:
    """Bei genau einem Abzug stand dieselbe Zahl zweimal untereinander:
    „Abzüge ≈ −512.30" und direkt darunter „Sozialabzüge total ≈ 512.30"."""
    tx_id = _gutschrift("4735.00", "Einzelabzug Norwyn")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "Sozialabzuege total", "265.00", LohnHerkunft.ERFASST),
    ])
    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Einzelabzug Norwyn"}).text)
    assert block.count("265.00") == 1, (
        f"Derselbe Abzug steht zweimal im Block: {block!r}")
    assert "Sozialabzuege total" in block, "Die Bezeichnung des Abzugs fehlt"


def test_positive_differenz_traegt_ihr_vorzeichen(logged_in_client: TestClient) -> None:
    """„Differenz 67.70" liess offen, in welche Richtung es fehlt — `chf_wert`
    setzt nur das Minus, und das Etikett benennt die Richtung nicht."""
    tx_id = _gutschrift("4600.00", "Vorzeichenprobe Elarion")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.ERFASST),
    ])
    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Vorzeichenprobe Elarion"}).text)
    assert "lohn-differenz" in block, "Vorbedingung: es gibt eine Differenz"
    zeile = block[block.index("lohn-differenz"):]
    assert "+135.00" in zeile, f"Die positive Differenz steht ohne Plus da: {zeile!r}"


def test_alter_betrag_ueberlebt_einen_validierungsfehler() -> None:
    """Nach einem 400er schrieb das Formular den FRISCH GETIPPTEN Wert als „alt".

    Die Herkunfts-Prüfung verglich die Zahl danach mit sich selbst: wer den
    Bruttolohn änderte und im selben Zug eine Bezeichnung vergass, bekam nach
    dem Nachtragen einen selbst getippten Wert als „gerechnet" gespeichert — mit
    „≈" in der Anzeige. Der Vertrag steht in der Vorlage, die dieser Test liest:
    ``lohn_alt`` trägt den GESPEICHERTEN Wert, nie den frisch getippten.
    """
    vorlage = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
               / "partials" / "lohn_editor.html").read_text(encoding="utf-8")
    assert 'name="lohn_alt" value="{{ row.betrag }}"' not in vorlage, (
        "lohn_alt kommt wieder aus dem getippten Betrag statt aus dem alten Stand")
    assert "row.alt" in vorlage, "Das Formular gibt den alten Betrag nicht zurueck"

    router = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "routers"
              / "transactions.py").read_text(encoding="utf-8")
    stelle = router[router.index("eingetippt = ["):]
    stelle = stelle[:stelle.index("]")]
    assert '"alt"' in stelle, (
        f"Der Router gibt lohn_alt nicht ans Formular zurueck: {stelle!r}")


# ---------------------------------------------------------------------------
# Die Sätze kommen aus den Daten des Nutzers, nicht aus dem Code
# ---------------------------------------------------------------------------


def test_editor_bietet_jeden_abzug_der_lohnabrechnung_an() -> None:
    """Was nicht in der Vorschlagsliste steht, wird jeden Monat neu erfunden.

    NBUV und KTG zieht der Arbeitgeber ab wie AHV und ALV. Fehlten sie in der
    Liste, war schon die erste Erfassung unvollständig — und aus genau dieser
    ersten Erfassung speist sich jeder weitere Monat. Wer die Zeile stattdessen
    von Hand nachträgt, benennt sie in jedem Monat etwas anders, und die Monate
    lassen sich nicht mehr nebeneinanderlegen.

    Die Beträge dazu stehen bewusst NICHT im Code: sie hängen am Arbeitgeber.
    Die Liste bietet die Zeile an, den Wert trägt der Nutzer ein.
    """
    _alle_abrechnungen_weg()
    tx_id = _gutschrift("4150.00", "Leerer Vorschlag Thalen", tag=date(2017, 3, 25))
    with SessionLocal() as db:
        zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, tx_id))

    assert grundlage is None, "Vorbedingung: es gibt keine Quelle, aus der abgeleitet wird"
    labels = [z["label"] for z in zeilen]
    fehlend = [p for p in ("NBUV", "KTG", "Pensionskasse") if p not in labels]
    assert not fehlend, f"Diese Abzüge fehlen in der Vorschlagsliste: {fehlend} — da steht {labels}"
    assert all(z["betrag"] == "" for z in zeilen), "ohne Quelle darf kein Wert erfunden werden"


def test_vorschlag_bevorzugt_den_von_hand_erfassten_monat() -> None:
    """Ein Vorschlag hängt sich an den Monat, in dem jemand etwas ABGELESEN hat.

    Vorher nahm er schlicht den zuletzt gespeicherten — und der ist, sobald ein
    Vorschlag einmal unverändert gespeichert wurde, selbst nur eine Kopie. Die
    Kette liefe von Kopie zu Kopie: „übernommen aus Juli" nennte einen Monat, in
    dem nie jemand ein Lohnblatt in der Hand hatte, und eine Korrektur am
    erfassten Monat erreichte die späteren Vorschläge nie.

    Daran hängt der ganze Weg, den das Modul anbietet: einmal von Hand erfassen,
    danach speist sich jeder Monat daraus.
    """
    _alle_abrechnungen_weg()
    von_hand = _gutschrift("4150.00", "Handerfassung Sarnen", tag=date(2018, 6, 25))
    _mit_posten(von_hand, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.ERFASST),
    ])
    # Derselbe Vorschlag, unverändert gespeichert: JEDER Posten gerechnet.
    kopie = _gutschrift("4150.00", "Kopie Sarnen", tag=date(2018, 7, 25))
    _mit_posten(kopie, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.GERECHNET),
    ])
    naechster = _gutschrift("4150.00", "Naechster Sarnen", tag=date(2018, 8, 25))

    with SessionLocal() as db:
        _zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, naechster))

    assert grundlage == "fortgeschrieben aus Juni 2018", (
        f"Der Vorschlag hängt an der Kopie statt am erfassten Monat: {grundlage!r}")


def test_editor_nennt_den_monat_aus_dem_der_vorschlag_stammt(
    logged_in_client: TestClient,
) -> None:
    """Eine übernommene Zahl ohne ihren Herkunftsmonat ist nicht nachvollziehbar.

    Der Editor zeigt vorbelegte Beträge, die es für DIESEN Monat noch nirgends
    gibt. Steht nicht dabei, aus welchem Monat sie stammen, sehen sie aus wie
    eine Erfassung — und wer sie speichert, bestätigt eine Herkunft, die er
    nicht kennt.
    """
    _alle_abrechnungen_weg()
    quelle = _gutschrift("4150.00", "Quelle Aeryn", tag=date(2016, 5, 25))
    _mit_posten(quelle, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
    ])
    ziel = _gutschrift("4150.00", "Ziel Aeryn", tag=date(2016, 6, 25))

    seite = logged_in_client.get(f"/transactions?form=edit&id={ziel}").text
    editor = seite[seite.index('id="lohn-editor"'):]
    editor = editor[:editor.index("</form>")]
    assert "fortgeschrieben aus Mai 2016" in editor, (
        f"Der Editor sagt nicht, aus welchem Monat die Zahlen stammen: {editor!r}")


# ---------------------------------------------------------------------------
# Die mitlaufende Gegenprobe des Editors
#
# Sie ist die Darstellung, in der der Nutzer ENTSCHEIDET — und sie rechnete
# lange im Browser, nach eigenen Regeln. Geprüft wurde das über Zeichenketten in
# app.js; wer die Zeichenketten stehen liess und die Bedeutung drehte, kam durch
# (Abzüge positiv gezählt, „auf dem Konto" fest 0.00, das ≈ an jeder Zahl). Die
# Rechnung steht jetzt auf dem Server, hinter einer Route — und damit hier unter
# denselben Tests wie der Aufklapper an der Buchung.
# ---------------------------------------------------------------------------


def _gegenprobe(client: TestClient, tx_id: int,
                zeilen: list[tuple[str, str, str, str, str]]) -> str:
    """Die Antwort der Gegenprobe auf Editor-Zeilen.

    Eine Zeile ist (Art, Bezeichnung, Betrag, Betrag VOR der Bearbeitung,
    Herkunft VOR der Bearbeitung) — genau die Felder, die das Formular mitsendet.
    """
    r = client.post(f"/transactions/{tx_id}/lohn/probe", data={
        "lohn_art": [z[0] for z in zeilen],
        "lohn_label": [z[1] for z in zeilen],
        "lohn_key": [f"z{i}" for i in range(len(zeilen))],
        "lohn_betrag": [z[2] for z in zeilen],
        "lohn_alt": [z[3] for z in zeilen],
        "lohn_herkunft": [z[4] for z in zeilen],
    })
    assert r.status_code == 200
    return r.text


def _marke(html: str, nr: int) -> str:
    """Was die Gegenprobe neben das Betragsfeld der Zeile ``nr`` zurückschickt."""
    anker = f'id="lohn-marke-z{nr}"'
    assert anker in html, f"Die Gegenprobe äussert sich zu Zeile {nr} gar nicht: {html!r}"
    rest = html[html.index(anker):]
    return rest[rest.index(">") + 1:rest.index("</span>")]


def test_vorschau_zieht_abzuege_ab_und_stellt_den_gebuchten_betrag_daneben(
    logged_in_client: TestClient,
) -> None:
    """Nettolohn = Brutto − Abzüge, verglichen mit dem Betrag DIESER Buchung.

    Zwei Sabotagen überlebten die alte Prüfung: Abzüge positiv gezählt (der
    Nettolohn der Vorschau war Brutto + Abzüge) und „auf dem Konto" fest auf
    0.00, womit die Differenz immer der volle Nettolohn war.
    """
    tx_id = _gutschrift("4150.00", "Gegenprobe Iselin")
    html = _gegenprobe(logged_in_client, tx_id, [
        ("brutto", "Bruttolohn", "5000.00", "", ""),
        ("abzug", "AHV/IV/EO", "800.00", "", ""),
    ])
    assert "4&#39;200.00" in html, f"Der Abzug wurde nicht abgezogen: {html!r}"
    assert "4&#39;150.00" in html, f"Der gebuchte Betrag steht nicht daneben: {html!r}"
    assert "+50.00" in html, f"Die Differenz stimmt nicht: {html!r}"


def test_vorschau_kennzeichnet_jeden_uebernommenen_wert(
    logged_in_client: TestClient,
) -> None:
    """Nicht nur die Summe trägt ein „≈", sondern jeder übernommene Betrag.

    Vorher markierte der Editor ausschliesslich den Nettolohn. Die vorbelegten
    Eingabefelder darüber sahen aus wie getippte Zahlen, und die Legende
    „≈ gerechnet, nicht abgelesen" stand über genau einer Marke — sie versprach
    mehr, als sie kennzeichnete.
    """
    tx_id = _gutschrift("4150.00", "Marke Iselin")
    html = _gegenprobe(logged_in_client, tx_id, [
        ("brutto", "Bruttolohn", "5000.00", "5000.00", "gerechnet"),
        ("abzug", "AHV/IV/EO", "800.00", "800.00", "gerechnet"),
    ])
    assert _marke(html, 0) == "≈", f"Der übernommene Bruttolohn steht unmarkiert da: {html!r}"
    assert _marke(html, 1) == "≈", f"Der übernommene Abzug steht unmarkiert da: {html!r}"
    assert "≈ gerechnet, nicht abgelesen" in html, "Die Legende fehlt"
    netto = html[html.index("Nettolohn"):]
    assert '<span class="lohn-marke">≈</span> 4&#39;200.00' in netto[:250], (
        f"Die Summe traegt keine Marke: {netto[:250]!r}")


def test_vorschau_verliert_die_marke_beim_aendern_einer_zahl(
    logged_in_client: TestClient,
) -> None:
    """„Gerechnet" ist keine Eigenschaft der ZEILE, sondern des unveränderten
    Betrags — dieselbe Regel und dieselbe Funktion wie beim Speichern.

    Das mitgesendete ``lohn_herkunft`` trägt den Stand VOR der Bearbeitung und
    ändert sich beim Tippen nicht. Hinge die Marke allein daran, klebte das „≈"
    an einer selbst getippten Zahl: die Vorschau behauptete eine Herkunft, die
    der Server beim Speichern gar nicht vergibt.
    """
    tx_id = _gutschrift("4150.00", "Marke geaendert Iselin")
    eine = _gegenprobe(logged_in_client, tx_id, [
        ("brutto", "Bruttolohn", "5000.00", "5000.00", "gerechnet"),
        ("abzug", "AHV/IV/EO", "812.50", "800.00", "gerechnet"),
    ])
    assert _marke(eine, 0) == "≈", "Die unveränderte Zeile verliert ihre Marke"
    assert _marke(eine, 1) == "", f"Die geänderte Zahl gilt weiter als gerechnet: {eine!r}"
    assert "≈ gerechnet, nicht abgelesen" in eine, "Eine gerechnete Zeile ist noch da"

    alle = _gegenprobe(logged_in_client, tx_id, [
        ("brutto", "Bruttolohn", "5100.00", "5000.00", "gerechnet"),
        ("abzug", "AHV/IV/EO", "812.50", "800.00", "gerechnet"),
    ])
    assert _marke(alle, 0) == "" and _marke(alle, 1) == ""
    assert "gerechnet, nicht abgelesen" not in alle, (
        f"Die Legende steht über lauter selbst getippten Zahlen: {alle!r}")
    assert "≈" not in alle, (
        f"Selbst getippte Zahlen tragen die Marke der Ableitung: {alle!r}")


def test_vorschau_und_aufklapper_zeigen_dieselbe_differenz(
    logged_in_client: TestClient,
) -> None:
    """Dieselben Zahlen, dieselbe Aussage — beide durch denselben Baustein.

    Gemessen an denselben Werten sagte der Aufklapper „+135.00", die Vorschau
    „135.00": im Feld, in dem der Nutzer entscheidet, blieb offen, in welche
    Richtung es fehlt. Genau dafür gibt es das Pluszeichen einen Test weiter oben.
    """
    tx_id = _gutschrift("4600.00", "Vorzeichen beidseits Elarion")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Bruttolohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.ERFASST),
    ])
    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Vorzeichen beidseits Elarion"}).text)
    vorschau = _gegenprobe(logged_in_client, tx_id, [
        ("brutto", "Bruttolohn", "5000.00", "5000.00", "erfasst"),
        ("abzug", "AHV/IV/EO", "265.00", "265.00", "erfasst"),
    ])
    assert "+135.00" in block, f"Der Aufklapper lässt die Richtung offen: {block!r}"
    assert "+135.00" in vorschau, f"Die Vorschau lässt die Richtung offen: {vorschau!r}"


def test_vorschau_rechnet_nicht_mit_eingaben_die_der_server_ablehnt(
    logged_in_client: TestClient,
) -> None:
    """Wo es kein Ergebnis geben wird, steht der Grund statt einer Zahl.

    Im Browser gemessen: ein negativer Bruttolohn ergab in der Vorschau einen
    Nettolohn samt Differenz, obwohl die Route ihn mit 400 abweist; alle Felder
    leer ergab ebenfalls ein Ergebnis, obwohl das Speichern die Aufstellung dann
    entfernt. Die Vorschau ist die Stelle, an der entschieden wird.
    """
    tx_id = _gutschrift("4150.00", "Abgelehnt Iselin")
    # Kein Betrag im Markup heisst: kein behauptetes Ergebnis. Auf den Fliesstext
    # zu prüfen ginge daneben — die Begründung nennt „Nettolohn" selbst.
    ohne_zahl = 'class="lohn-betrag"'

    negativ = _gegenprobe(logged_in_client, tx_id,
                          [("brutto", "Bruttolohn", "-100.00", "", "")])
    assert ohne_zahl not in negativ, f"Ein abgelehnter Betrag ergibt ein Ergebnis: {negativ!r}"
    assert "sind positiv" in negativ, f"Der Grund fehlt: {negativ!r}"

    leer = _gegenprobe(logged_in_client, tx_id, [("brutto", "Bruttolohn", "", "", "")])
    assert ohne_zahl not in leer, f"Leere Felder ergeben ein Ergebnis: {leer!r}"

    ohne_brutto = _gegenprobe(logged_in_client, tx_id,
                              [("abzug", "AHV/IV/EO", "800.00", "", "")])
    assert ohne_zahl not in ohne_brutto, f"Ohne Bruttolohn ein Ergebnis: {ohne_brutto!r}"
    assert "Ohne Bruttolohn" in ohne_brutto, f"Der Grund fehlt: {ohne_brutto!r}"


def test_vorschau_liest_schweizer_betraege_vollstaendig(
    logged_in_client: TestClient,
) -> None:
    """Ein Betrag mit ZWEI Apostrophen darf nicht auf halbem Weg abbrechen.

    Gemessen, als der Editor selbst rechnete: „1'250'000.00" ergab einen um
    Faktor 1000 falschen Nettolohn mit falschem Vorzeichen bei der Differenz,
    weil nur der erste Apostroph verschwand. Die App druckt Beträge überall mit
    Apostroph — das Format ist naheliegend, nicht exotisch.
    """
    tx_id = _gutschrift("4150.00", "Apostroph Iselin")
    html = _gegenprobe(logged_in_client, tx_id,
                       [("brutto", "Bruttolohn", "1'250'000.00", "", "")])
    assert "1&#39;250&#39;000.00" in html, f"Der Betrag wurde verstümmelt: {html!r}"


def test_aufklapper_ist_ein_tippziel() -> None:
    """Der Aufklapper mass 28px, weil der 44px-Wächter Klassen aufzählte.

    Er tut es nicht mehr: die Regel greift an ``details > summary``, also an der
    Bauform. Dieser Test prüft weiterhin dieselbe Zusage — der Lohn-Aufklapper
    bekommt sein Tippziel —, nur nicht mehr über seinen Namen. Er wäre sonst
    grün, während ein anderer Aufklapper durchfällt, und rot, sobald jemand die
    Liste zugunsten der allgemeinen Regel aufräumt.
    """
    import re as _re

    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")
    block = css[css.index("Touch-Ziele"):]
    block = block[:block.index("min-height: 44px;")]
    # Kommentare RAUS, bevor gesucht wird: im Kommentar dieser Regel stehen
    # Klassennamen als Begruendung, und der Test blieb dadurch gruen, als der
    # Selektor selbst entfernt wurde — nachgemessen.
    selektoren = _re.sub(r"/\*.*?\*/", "", block, flags=_re.S)
    assert "details:not(.rowmenu) > summary" in selektoren, (
        "Der Lohn-Aufklapper ist ein <summary> und bekommt sein Tippziel ueber "
        "die Bauform. Fehlt diese Regel, faellt er auf 28px zurueck.")
    # Und er ist wirklich ein <summary> — sonst prueft die Regel oben nichts.
    markup = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
              / "partials").rglob("*.html")
    assert any("<summary class=\"lohn-kopf\"" in p.read_text(encoding="utf-8")
               for p in markup), "Der Lohnblock hat keinen <summary>-Aufklapper mehr."
def test_bezeichnung_im_block_wird_nicht_abgeschnitten() -> None:
    """``.lohn-was`` stand einzeilig mit Ellipse.

    Bei 375px bleiben der Bezeichnung 176px; „Monatslohn brutto (80% Pensum)"
    braucht 221px — angezeigt wurde „Monatslohn brutto (80% …". Ausgerechnet das
    Wort, das die Zeile erklärt, fiel weg, und erreichbar war es nirgends: kein
    Tooltip, kein zweiter Ort. Genau diese Bezeichnung begründet die
    Einzelposten-Regel ein paar Tests weiter oben.
    """
    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")
    regel = css[css.index(".lohn-was {"):]
    regel = regel[:regel.index("}")]
    assert "nowrap" not in regel, (
        f"Die Bezeichnung steht wieder einzeilig und verliert ihr Ende: {regel!r}")


# ---------------------------------------------------------------------------
# Woher ein Vorschlag stammt — und was er über sich behauptet
# ---------------------------------------------------------------------------


def test_uebernommener_monat_wird_fortgeschrieben_und_nicht_erfasst() -> None:
    """Die zentrale Zusage des Moduls: eine Kopie behauptet nicht, abgelesen zu sein.

    Ein früherer Monat wird als Vorschlag übernommen — auch dann, wenn dort JEDER
    Posten vom Nutzer erfasst war. Für DIESEN Monat ist keine der Zahlen belegt.
    Fiele die Kennzeichnung weg, verschwände das Zeichen überall, die Aufstellung
    behauptete abgelesen zu sein, und die Vorauswahl in ``_letzte_abrechnung``
    liefe wieder von Kopie zu Kopie: jede Kopie zählte danach selbst als „selbst
    erfasst".

    ``FORTGESCHRIEBEN`` und nicht ``GERECHNET``: der Wert steht exakt so auf
    einem Blatt, nur eben auf dem des Juni. Ihn mit einer aus dem Jahreslohn
    geteilten Schätzung gleichzusetzen macht die Kennzeichnung wertlos — sie
    stünde dann an fast jeder Zahl der App.
    """
    _alle_abrechnungen_weg()
    quelle = _gutschrift("4150.00", "Abgelesen Sarnen", tag=date(2018, 6, 25))
    _mit_posten(quelle, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.ERFASST),
    ])
    ziel = _gutschrift("4150.00", "Uebernahme Sarnen", tag=date(2018, 7, 25))

    with SessionLocal() as db:
        zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, ziel))

    assert grundlage == "fortgeschrieben aus Juni 2018", "Vorbedingung: übernommen wird der Juni"
    assert [z["herkunft"] for z in zeilen] == ["fortgeschrieben", "fortgeschrieben"], (
        f"Eine Kopie gibt sich als abgelesen oder als geschätzt aus: {zeilen!r}")


def test_vorschlag_nimmt_keinen_monat_aus_der_zukunft() -> None:
    """Ein späterer Monat weiss noch nichts von diesem.

    Die Datumsschranke galt nur in der ersten Stufe; zwei schrankenlose Stufen
    dahinter fingen jede nachgetragene Buchung wieder ein. Gemessen an einer
    Buchung aus einem lange vergangenen Jahr: ein Vorschlag „übernommen aus"
    einem Monat elf Jahre später, alle sieben Posten vorbelegt.
    """
    _alle_abrechnungen_weg()
    spaet = _gutschrift("4150.00", "Spaeter Norwyn", tag=date(2024, 6, 25))
    _mit_posten(spaet, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
    ])
    frueh = _gutschrift("4150.00", "Frueher Norwyn", tag=date(2013, 1, 25))

    with SessionLocal() as db:
        _zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, frueh))

    assert "übernommen aus" not in (grundlage or ""), (
        f"Der Vorschlag stammt aus der Zukunft der Buchung: {grundlage!r}")


def test_vorschlag_nimmt_die_buchung_nicht_als_eigene_quelle() -> None:
    """Eine Buchung darf sich nicht selbst als Herkunft vorschlagen.

    Sonst schriebe der Knopf „Vorschlag" ihre eigenen, abgelesenen Posten auf
    GERECHNET um und behauptete dazu, sie stammten aus ihrem eigenen Monat.
    """
    _alle_abrechnungen_weg()
    fremd = _gutschrift("4150.00", "Fremdquelle Thalen", tag=date(2020, 4, 25))
    _mit_posten(fremd, [
        (LohnPostenArt.BRUTTO, "Fremder Posten", "5000.00", LohnHerkunft.ERFASST),
    ])
    selbst = _gutschrift("4150.00", "Eigenquelle Thalen", tag=date(2020, 5, 25))
    _mit_posten(selbst, [
        (LohnPostenArt.BRUTTO, "Eigener Posten", "4200.00", LohnHerkunft.ERFASST),
    ])

    with SessionLocal() as db:
        zeilen, grundlage = lohn_service.vorschlag(db, db.get(Transaction, selbst))

    assert [z["label"] for z in zeilen] == ["Fremder Posten"], (
        f"Die Buchung schlaegt sich selbst vor: {zeilen!r}")
    assert grundlage == "fortgeschrieben aus April 2020"


def test_pensionskassenbeitrag_kommt_aus_dem_vorsorgeausweis() -> None:
    """Die einzige Stelle, an der ein Wert aus dem Vorsorgeausweis in einen
    Vorschlag gelangt.

    Fällt sie stillschweigend aus, steht die Pensionskassen-Zeile leer da und
    sieht damit aus wie „dafür gibt es keine Quelle" — obwohl es eine gibt. Die
    Unterscheidung leer/vorhanden ist der ganze Punkt dieser Zeile.
    """
    _alle_abrechnungen_weg()
    tx_id = _gutschrift("4150.00", "Vorsorge Kaeldra", tag=date(2021, 5, 25))
    with SessionLocal() as db:
        lohnreihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "lohn"))
        vorsorge = db.scalar(select(MetricSeries).where(MetricSeries.slug == "pk_guthaben"))
        jahr = MetricPoint(series_id=lohnreihe.id, period_start=date(2021, 1, 1),
                           period_end=date(2021, 12, 31), value=Decimal("60000.00"))
        ausweis = MetricPoint(series_id=vorsorge.id, period_start=date(2021, 1, 1),
                              period_end=date(2021, 1, 1), value=Decimal("20000.00"),
                              extras={"beitrag_monat": "400.00"})
        db.add_all([jahr, ausweis])
        db.commit()
        try:
            zeilen, _grundlage = lohn_service.vorschlag(db, db.get(Transaction, tx_id))
        finally:
            db.delete(jahr)
            db.delete(ausweis)
            db.commit()

    nach_label = {z["label"]: z for z in zeilen}
    assert nach_label["Pensionskasse"]["betrag"] == "400.00", (
        f"Der Monatsbeitrag aus dem Vorsorgeausweis fehlt: {zeilen!r}")
    assert nach_label["NBUV"]["betrag"] == "", "ohne Quelle bleibt die Zeile leer"


def test_vorschlagsliste_kennt_den_anteil_am_dreizehnten_monatslohn() -> None:
    """Der Anteil am 13. Monatslohn steht als eigene Position auf dem Lohnblatt.

    Er wird anteilig mit jeder Monatszahlung ausgerichtet — er ist also keine
    Besonderheit des Dezembers, sondern eine Zeile, die jeder Monat hat. Genau
    das begründet der Kommentar an ``MONATE_IM_JAHR``, während die
    Vorschlagsliste die Zeile nicht anbot. Ohne sie legt der Nutzer sie jeden
    Monat neu an und benennt sie jedes Mal anders; die Monate lassen sich danach
    nicht mehr nebeneinanderlegen.

    Wie hoch der Anteil ist, steht im Arbeitsvertrag und darum NICHT im Code:
    die Zeile wird angeboten, den Wert trägt der Nutzer ein.
    """
    _alle_abrechnungen_weg()
    ohne_quelle = _gutschrift("4150.00", "Dreizehnter leer Aeryn", tag=date(2014, 3, 25))
    aus_jahreswert = _gutschrift("4150.00", "Dreizehnter Jahr Aeryn", tag=date(2022, 3, 25))
    with SessionLocal() as db:
        leer_zeilen, leer_grundlage = lohn_service.vorschlag(
            db, db.get(Transaction, ohne_quelle))
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "lohn"))
        punkt = MetricPoint(series_id=reihe.id, period_start=date(2022, 1, 1),
                            period_end=date(2022, 12, 31), value=Decimal("60000.00"))
        db.add(punkt)
        db.commit()
        try:
            jahr_zeilen, _g = lohn_service.vorschlag(db, db.get(Transaction, aus_jahreswert))
        finally:
            db.delete(punkt)
            db.commit()

    assert leer_grundlage is None, "Vorbedingung: es gibt keine Quelle, aus der abgeleitet wird"
    for woher, zeilen in (("ohne Quelle", leer_zeilen), ("aus dem Jahreswert", jahr_zeilen)):
        nach_label = {z["label"]: z for z in zeilen}
        assert "Anteil 13. Monatslohn" in nach_label, (
            f"Die Position fehlt im Vorschlag {woher}: {[z['label'] for z in zeilen]}")
        anteil = nach_label["Anteil 13. Monatslohn"]
        assert anteil["art"] == "brutto", "Der Anteil gehört auf die Brutto-Seite"
        assert anteil["betrag"] == "", "ohne Quelle darf kein Wert erfunden werden"


def test_aufklapper_nennt_die_grundlage_der_gerechneten_werte(
    logged_in_client: TestClient,
) -> None:
    """Der Herkunftsmonat muss auch NACH dem Speichern dastehen.

    Der Editor nennt ihn (eigener Test weiter oben) — aus der Anzeige an der
    Buchung liess sich dieselbe Zeile ersatzlos entfernen, ohne dass ein Test
    rot wurde. Eine übernommene Zahl ohne ihren Herkunftsmonat ist nicht
    nachvollziehbar: sie sieht dann aus wie eine Erfassung.

    Das ``title`` prüft mit, weil die Zeile auf zwei Zeilen begrenzt ist — ohne
    es wäre eine lange Grundlage nirgends mehr vollständig erreichbar.
    """
    tx_id = _gutschrift("4150.00", "Grundlage Aeryn")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.GERECHNET),
    ], grundlage="übernommen aus Mai 2016")

    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Grundlage Aeryn"}).text)
    assert "Grundlage: übernommen aus Mai 2016" in block, (
        f"Der Herkunftsmonat fehlt in der Anzeige: {block!r}")
    assert 'title="übernommen aus Mai 2016"' in block, (
        f"Eine abgeschnittene Grundlage waere nicht erreichbar: {block!r}")


# ---------------------------------------------------------------------------
# Die dritte Stufe: fortgeschrieben
#
# Ein Lohnblatt gibt es nur bei einer LOHNAENDERUNG. Der Normalfall ist der
# Monat dazwischen: seine Zahlen sind abgelesen, nur eben in einem frueheren
# Monat. Vorher liefen sie als „gerechnet" mit — zusammen mit allem, was aus
# einem Jahreswert geteilt wurde. Eine Marke, die an fast jeder Zahl steht,
# unterscheidet nichts mehr.
# ---------------------------------------------------------------------------


def test_kopie_eines_gerechneten_postens_bleibt_gerechnet() -> None:
    """Abschreiben macht aus einer Schätzung keine Ablesung.

    Die Trennlinie zwischen den beiden unteren Stufen ist, ob ein Wert auf ein
    Blatt zurückgeht. Der Monatslohn eines erfassten Monats tut das und wird
    fortgeschrieben; die aus einem Beitragssatz gerechnete AHV-Zeile tut es nie
    — sie darf beim Übernehmen nicht aufsteigen. Wäre die Regel „alles aus einer
    erfassten Aufstellung wird fortgeschrieben", trüge nach zwei Monaten jede
    gerechnete Zahl der App die Marke einer abgelesenen.
    """
    _alle_abrechnungen_weg()
    quelle = _gutschrift("4150.00", "Gemischt Aeryn", tag=date(2015, 4, 25))
    _mit_posten(quelle, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.GERECHNET),
    ])
    ziel = _gutschrift("4150.00", "Gemischt Ziel Aeryn", tag=date(2015, 5, 25))

    with SessionLocal() as db:
        zeilen, _grundlage = lohn_service.vorschlag(db, db.get(Transaction, ziel))

    nach_label = {z["label"]: z["herkunft"] for z in zeilen}
    assert nach_label["Monatslohn"] == "fortgeschrieben", (
        f"Der abgelesene Posten steigt nicht auf: {zeilen!r}")
    assert nach_label["AHV/IV/EO"] == "gerechnet", (
        f"Eine gerechnete Zahl gibt sich nach dem Kopieren als abgelesen aus: {zeilen!r}")


def test_die_drei_stufen_sind_an_verschiedenen_zeichen_zu_unterscheiden() -> None:
    """Ohne eigenes Zeichen wäre die dritte Stufe im Datenmodell erfunden.

    Sie steht dann in der Datenbank und nirgends auf dem Schirm — die Anzeige
    zeigte den Normalfall weiter wie eine Schätzung. Erfasst trägt bewusst KEIN
    Zeichen: eine Zahl ohne Marke ist eine, die auf dem Blatt dieses Monats
    steht, und das ist der ruhigste Fall.
    """
    zeichen = lohn_service.MARKE
    assert zeichen[LohnHerkunft.ERFASST] == ""
    assert zeichen[LohnHerkunft.FORTGESCHRIEBEN], "Die dritte Stufe hat kein Zeichen"
    assert zeichen[LohnHerkunft.GERECHNET], "Die gerechnete Stufe hat kein Zeichen"
    assert (
        zeichen[LohnHerkunft.FORTGESCHRIEBEN] != zeichen[LohnHerkunft.GERECHNET]
    ), "Zwei Stufen, ein Zeichen — dann trennt die Anzeige sie nicht"


def test_summenzeile_erbt_die_schwaechste_stufe_ihrer_posten() -> None:
    """Eine Summe ist nie genauer als ihr unsicherster Summand.

    Ein Bruttolohn aus einem fortgeschriebenen Monatslohn plus einer gerechneten
    Zulage ist gerechnet. Erbte die Summe stattdessen die BESTE Stufe, trüge
    ausgerechnet die Zeile, auf die man schaut, weniger Vorbehalt als jede Zeile
    darunter.
    """
    tx_id = _gutschrift("4150.00", "Summenstufe Iselin")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.FORTGESCHRIEBEN),
        (LohnPostenArt.BRUTTO, "Zulage", "42.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.FORTGESCHRIEBEN),
    ])
    with SessionLocal() as db:
        a = lohn_service.aufstellung(
            lohn_service.abrechnung_zu(db, tx_id), db.get(Transaction, tx_id))

    assert a.brutto_marke == lohn_service.MARKE[LohnHerkunft.GERECHNET], (
        "Die Brutto-Summe verschweigt ihren gerechneten Summanden")
    assert a.abzuege_marke == lohn_service.MARKE[LohnHerkunft.FORTGESCHRIEBEN], (
        "Die Abzugs-Summe behauptet weniger Kenntnis, als vorhanden ist")
    assert a.netto_marke == lohn_service.MARKE[LohnHerkunft.GERECHNET]


def test_unveraenderter_fortgeschriebener_posten_faellt_nicht_auf_gerechnet_zurueck() -> None:
    """Die Stufe muss einen Durchlauf durchs Formular überleben.

    Beim Speichern wird die Herkunft aus dem mitgesendeten Stand neu bestimmt.
    Kennt diese Regel nur „gerechnet oder erfasst", verliert ein fortgeschriebener
    Monat seine Stufe beim ersten Speichern wieder — die Unterscheidung hielte
    keinen einzigen Durchlauf aus, und die Migration hätte umsonst umgeschrieben.
    """
    unveraendert = lohn_service.herkunft_nach_aenderung(
        Decimal("111.11"), Decimal("111.11"), "fortgeschrieben")
    veraendert = lohn_service.herkunft_nach_aenderung(
        Decimal("222.22"), Decimal("111.11"), "fortgeschrieben")

    assert unveraendert == LohnHerkunft.FORTGESCHRIEBEN
    assert veraendert == LohnHerkunft.ERFASST, "Wer eine Zahl ändert, verantwortet sie"


def test_legende_nennt_jede_vorkommende_stufe_genau_einmal(
    logged_in_client: TestClient,
) -> None:
    """Zwei Zeichen brauchen zwei Erklärungen — aber je eine, nicht je Zeile.

    An jede Zeile gehängt stünde derselbe Halbsatz bis zu acht Mal untereinander,
    und der Block ist auf 375px ohnehin das längste Element der Buchungsliste.
    Umgekehrt darf keine Stufe fehlen: ein Zeichen ohne Erklärung ist ein Rätsel.
    """
    tx_id = _gutschrift("4150.00", "Legende Norwyn")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.FORTGESCHRIEBEN),
        (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.GERECHNET),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.ERFASST),
    ])
    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Legende Norwyn"}).text)

    for stufe in (LohnHerkunft.FORTGESCHRIEBEN, LohnHerkunft.GERECHNET):
        satz = f"{lohn_service.MARKE[stufe]} {lohn_service.LEGENDE[stufe]}"
        assert block.count(satz) == 1, (
            f"{satz!r} steht {block.count(satz)}× im Block statt genau einmal")


def test_legende_erklaert_keine_stufe_die_gar_nicht_vorkommt(
    logged_in_client: TestClient,
) -> None:
    """Eine Erklärung für ein Zeichen, das nirgends steht, ist Lärm.

    Der häufigste Block ist gemischt (fortgeschriebener Lohn, gerechnete
    Sozialabzüge). Ein vollständig fortgeschriebener Monat darf deshalb nicht
    trotzdem erklären, was „≈" bedeutet — dann trüge jeder Fuss beide Sätze,
    und die Legende sagte nichts mehr über DIESEN Block.
    """
    tx_id = _gutschrift("4150.00", "Nur fortgeschrieben Thalen")
    _mit_posten(tx_id, [
        (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.FORTGESCHRIEBEN),
        (LohnPostenArt.ABZUG, "NBUV", "11.11", LohnHerkunft.FORTGESCHRIEBEN),
    ])
    block = _lohnblock(
        logged_in_client.get("/transactions", params={"q": "Nur fortgeschrieben Thalen"}).text)

    assert lohn_service.LEGENDE[LohnHerkunft.FORTGESCHRIEBEN] in block
    assert lohn_service.LEGENDE[LohnHerkunft.GERECHNET] not in block, (
        f"Der Block erklärt ein Zeichen, das er nicht verwendet: {block!r}")


def test_marke_ist_in_allen_sechs_skins_lesbar() -> None:
    """Die kleinste Schrift des Blocks trägt seine wichtigste Aussage.

    Die Marke steht in 12px auf ``--bg-sunken``. Sie stand in ``--accent-primary``
    — nachgemessen erreicht das in ``ayu-hell`` nur 3.91:1 und liegt damit unter
    WCAG-AA für Kleintext. Mit der zweiten Stufe verdoppelte sich die betroffene
    Fläche, statt dass die Marke lesbar wird. Geprüft wird die Farbe, die die
    Regel wirklich setzt — nicht die, von der der Kommentar spricht.
    """
    from tests.test_skins import _kontrast, _parse_skins, _wert

    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")
    regel = css[css.index(".lohn-marke {"):]
    regel = regel[:regel.index("}")]
    treffer = re.search(r"color:\s*var\((--[a-z0-9-]+)\)", regel)
    assert treffer, f"Die Marke setzt keine Farbe über ein Token: {regel!r}"
    token = treffer.group(1)

    skins = _parse_skins()
    assert len(skins) == 6, f"Erwartet sind sechs Skins, gefunden: {sorted(skins)}"
    for skin in skins:
        grund, farbe = _wert(skins, skin, "--bg-sunken"), _wert(skins, skin, token)
        assert grund and farbe, f"{skin}: {token} oder --bg-sunken fehlt"
        k = _kontrast(farbe, grund)
        assert k >= 4.5, (
            f"{skin}: die Lohn-Marke ({token} {farbe}) erreicht auf dem Blatt "
            f"({grund}) nur {k:.2f}:1 — nötig sind 4.5:1 für 12px-Text")


# ---------------------------------------------------------------------------
# Die Jahresprobe
#
# Sie ist der einzige Ort, an dem sich eine fortgeschriebene Zahl WIDERLEGEN
# laesst: der Lohnausweis nennt ein ganzes Jahr in einer Zahl. Verglichen wird
# BRUTTO gegen BRUTTO — die Begruendung steht im Modul; kurz: einen
# Netto-Jahreswert gibt es in den Daten nicht, und auch nachgetragen liesse er
# sich nicht vergleichen, weil der Ausweis nicht jeden Abzug eines Lohnblatts
# kennt. Alle Betraege hier sind erfunden.
# ---------------------------------------------------------------------------


def _jahreswert(jahr: int, betrag: str) -> int:
    """Legt den Bruttolohn eines Jahres in der Reihe ``lohn`` ab. Gibt die Punkt-Id."""
    with SessionLocal() as db:
        reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == "lohn"))
        punkt = MetricPoint(series_id=reihe.id, period_start=date(jahr, 1, 1),
                            period_end=date(jahr, 12, 31), value=Decimal(betrag))
        db.add(punkt)
        db.commit()
        return punkt.id


def _punkt_weg(punkt_id: int) -> None:
    with SessionLocal() as db:
        punkt = db.get(MetricPoint, punkt_id)
        if punkt is not None:
            db.delete(punkt)
            db.commit()


def test_jahresprobe_geht_auf_wenn_die_monate_den_ausweis_ergeben() -> None:
    """Zwei Monatsbrutti, die zusammen den Jahreswert ergeben — das Jahr stimmt.

    Das ist die einzige Aussage, die die Probe positiv treffen darf, und sie
    braucht dafür keine Annahme über die Zahl der Monate: wenn die Summe passt,
    fehlt auch nichts.
    """
    _alle_abrechnungen_weg()
    punkt = _jahreswert(2011, "10000.00")
    try:
        for monat in (3, 4):
            tx_id = _gutschrift("4150.00", f"Probe auf Elarion {monat}",
                                tag=date(2011, monat, 25))
            _mit_posten(tx_id, [
                (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
                (LohnPostenArt.ABZUG, "AHV/IV/EO", "265.00", LohnHerkunft.GERECHNET),
            ])
        with SessionLocal() as db:
            proben = lohn_service.jahresproben(db, {2011})
    finally:
        _punkt_weg(punkt)

    probe = proben[2011]
    assert probe.summe == Decimal("10000.00"), "Nur die BRUTTO-Posten zählen mit"
    assert probe.gutschriften == 2
    assert probe.geht_auf is True
    assert "ergeben den Lohnausweis" in probe.befund, probe.befund


def test_jahresprobe_nennt_die_sonderzahlung_als_moeglichen_grund() -> None:
    """Der eine Fall, an dem eine naive Probe scheitern MUSS.

    Eine unregelmässige Leistung (Bonus, Weihnachtszulage) steckt im Jahreswert
    des Ausweises, steht aber auf keinem Monatsblatt. Eine Probe, die das nicht
    ausspricht, meldet in jedem solchen Jahr eine Abweichung, die keine ist —
    und wird nach dem zweiten Mal ignoriert. Aufzulösen ist sie, indem die
    Sonderzahlung ihre eigene Aufstellung bekommt: die Probe zählt GUTSCHRIFTEN,
    nicht Monate.
    """
    _alle_abrechnungen_weg()
    punkt = _jahreswert(2012, "11000.00")
    try:
        for monat in (3, 4):
            tx_id = _gutschrift("4150.00", f"Probe offen Elarion {monat}",
                                tag=date(2012, monat, 25))
            _mit_posten(tx_id, [
                (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.FORTGESCHRIEBEN),
            ])
        with SessionLocal() as db:
            proben = lohn_service.jahresproben(db, {2012})
    finally:
        _punkt_weg(punkt)

    probe = proben[2012]
    assert probe.geht_auf is False
    assert probe.differenz == Decimal("-1000.00")
    assert "unter dem Lohnausweis" in probe.befund, probe.befund
    assert "Sonderzahlung ohne Aufstellung" in probe.befund, (
        f"Die Sonderzahlung wird als Grund nicht genannt: {probe.befund!r}")
    assert "fortgeschriebener Monat" in probe.befund, (
        f"Der zweite mögliche Grund fehlt: {probe.befund!r}")


def test_jahresprobe_nennt_keinen_bonus_wenn_die_summe_zu_HOCH_ist() -> None:
    """Ein fehlender Bonus kann die Summe nicht über den Ausweis heben.

    Nennte die Probe ihn in beide Richtungen, stünde in der Hälfte der Fälle ein
    Grund da, der rechnerisch ausgeschlossen ist — und ein Befund, der auch
    falsche Gründe nennt, ist keiner.
    """
    _alle_abrechnungen_weg()
    punkt = _jahreswert(2010, "4000.00")
    try:
        tx_id = _gutschrift("4150.00", "Probe zu hoch Elarion", tag=date(2010, 3, 25))
        _mit_posten(tx_id, [
            (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
        ])
        with SessionLocal() as db:
            proben = lohn_service.jahresproben(db, {2010})
    finally:
        _punkt_weg(punkt)

    befund = proben[2010].befund
    assert "über dem Lohnausweis" in befund, befund
    assert "Sonderzahlung" not in befund, (
        f"Ein fehlender Bonus wird als Grund für eine ZU HOHE Summe genannt: {befund!r}")


def test_jahresprobe_rechnet_nicht_gegen_den_ausweis_eines_anderen_jahres() -> None:
    """Ohne Ausweis für DIESES Jahr gibt es keine Probe.

    Der Vorschlag für einen einzelnen Monat darf auf den letzten bekannten
    Jahreslohn zurückfallen — als Schätzgrundlage ist ein Vorjahreswert
    nachvollziehbar. Als Gegenprobe wäre er eine Behauptung über ein Jahr, dessen
    Ausweis niemand erfasst hat: die Probe meldete eine Abweichung, sobald der
    Lohn gestiegen ist.
    """
    _alle_abrechnungen_weg()
    punkt = _jahreswert(2008, "10000.00")
    try:
        tx_id = _gutschrift("4150.00", "Probe Vorjahr Elarion", tag=date(2009, 3, 25))
        _mit_posten(tx_id, [
            (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
        ])
        with SessionLocal() as db:
            proben = lohn_service.jahresproben(db, {2009})
    finally:
        _punkt_weg(punkt)

    assert proben == {}, f"Die Probe rechnet gegen ein fremdes Jahr: {proben!r}"


def test_jahresprobe_erfindet_kein_jahr_nach_dem_niemand_gefragt_hat() -> None:
    """Gefragt wird nach bestimmten Jahren, nicht nach einem Zeitraum.

    Geholt werden die Ausweiswerte in EINER Abfrage über die Spanne vom ersten
    bis zum letzten gefragten Jahr — sonst wäre es eine Abfrage je Jahr. Fällt
    danach der Filter weg, liefert die Probe auch Jahre dazwischen, nach denen
    niemand gefragt hat: gerechnet aus Aufstellungen, die auf der Seite gar
    nicht stehen. Die Buchungsliste zeigt gefilterte Jahre, und genau dann klafft
    die Lücke.
    """
    _alle_abrechnungen_weg()
    punkte = [_jahreswert(jahr, "5000.00") for jahr in (2001, 2002, 2003)]
    try:
        for jahr in (2001, 2002, 2003):
            tx_id = _gutschrift("4150.00", f"Luecke Sarnen {jahr}", tag=date(jahr, 3, 25))
            _mit_posten(tx_id, [
                (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.ERFASST),
            ])
        with SessionLocal() as db:
            proben = lohn_service.jahresproben(db, {2001, 2003})
    finally:
        for punkt in punkte:
            _punkt_weg(punkt)

    assert sorted(proben) == [2001, 2003], (
        f"Die Probe antwortet auf ein Jahr, nach dem niemand gefragt hat: {sorted(proben)}")


def test_jahresprobe_steht_im_block_an_der_buchung(logged_in_client: TestClient) -> None:
    """Sie gehört an die Quittung, nicht auf eine andere Seite.

    Der Block sagt sonst nur, wie diese eine Gutschrift zustande kam. Ob die
    fortgeschriebenen Zahlen darin noch stimmen, beantwortet erst das Jahr — und
    zwar dort, wo man die Zahlen sieht.
    """
    _alle_abrechnungen_weg()
    punkt = _jahreswert(2013, "5000.00")
    try:
        tx_id = _gutschrift("4150.00", "Jahresprobe Kaeldra", tag=date(2013, 3, 25))
        _mit_posten(tx_id, [
            (LohnPostenArt.BRUTTO, "Monatslohn", "5000.00", LohnHerkunft.FORTGESCHRIEBEN),
        ])
        # ``zeitraum=alles``: die Liste zeigt sonst nur das laufende Jahr, und
        # die Probe braucht ein Jahr, dessen Ausweiswert dieser Test selbst setzt.
        block = _lohnblock(logged_in_client.get(
            "/transactions", params={"q": "Jahresprobe Kaeldra", "zeitraum": "alles"}).text)
    finally:
        _punkt_weg(punkt)

    assert "2013" in block and "Lohnausweis" in block, (
        f"Die Jahresprobe fehlt im Block: {block!r}")
