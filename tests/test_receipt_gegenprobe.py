"""Die Gegenprobe des Beleg-Scans: ergeben die Positionen den Beleg-Total?

Der Anlass ist ein Vorfall. Der Scan schlug eine Positionsliste vor, die um eine
Zeile versetzt war — jede Zeile für sich plausibel, die Zuordnung Name→Preis
durchgehend falsch. Nichts hat es gemerkt; bestätigt wären falsche Beträge in
der Datenbank gelandet und hätten über die gelernten Regeln jeden weiteren Beleg
mitgezogen. Erst die Summe verrät so etwas.

Alle Belegtexte hier sind ERFUNDEN, Händler wie Beträge. Getestet wird die
Mechanik, nicht ein echter Einkauf.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import Attachment, Category, PendingReceipt, ReceiptItemRule
from moneten.db.session import SessionLocal
from moneten.services.receipt_digital import analyze, pruefe_positionen, save_receipt
from moneten.services.receipt_ocr import OcrResult


def _pos(*paare: tuple[str, str]) -> list[dict]:
    """Positionsliste in der Form, die der Vorschlagsdialog liefert."""
    return [{"name": n, "price": p} for n, p in paare]


def _first_category_id(db) -> int:
    return db.scalar(select(Category.id).where(Category.parent_id.is_not(None)))


# ---------------------------------------------------------------------------
# Die Probe selbst
# ---------------------------------------------------------------------------


def test_stimmige_liste_besteht() -> None:
    items, probe = pruefe_positionen(
        _pos(("Nussbrot", "4.20"), ("Hafermilch", "2.10")), Decimal("6.30")
    )
    assert probe.ok
    assert probe.summe == Decimal("6.30")
    assert probe.abweichung == Decimal("0.00")
    assert len(items) == 2


def test_versetzte_liste_faellt_durch() -> None:
    """Der Vorfall: Namen und Preise um eine Zeile verschoben.

    Der letzte Preis fehlt dann, der erste Name hat keinen — die Liste sieht
    vollständig aus und ist um genau eine Position zu billig.
    """
    _, probe = pruefe_positionen(
        _pos(("Nussbrot", "2.10"), ("Hafermilch", "7.90")), Decimal("14.20")
    )
    assert not probe.ok
    assert probe.abweichung == Decimal("-4.20")


def test_rappenrundung_besteht_zwei_rappen_darueber_nicht() -> None:
    """Zwei Rappen sind Rundung, drei sind ein Fehler.

    Bar bezahlte Bons werden auf 5 Rappen gerundet; die Positionen stehen in
    ganzen Rappen. Damit sind höchstens 2 Rappen Abweichung möglich. Die Grenze
    muss genau dort liegen — eine grosszügigere Toleranz verschluckte echte
    Lesefehler und machte die Probe wertlos.
    """
    positionen = _pos(("Nussbrot", "4.23"), ("Hafermilch", "2.10"))
    assert pruefe_positionen(positionen, Decimal("6.33"))[1].ok   # exakt
    assert pruefe_positionen(positionen, Decimal("6.35"))[1].ok   # +0.02
    assert pruefe_positionen(positionen, Decimal("6.31"))[1].ok   # -0.02
    assert not pruefe_positionen(positionen, Decimal("6.36"))[1].ok  # +0.03
    assert not pruefe_positionen(positionen, Decimal("6.30"))[1].ok  # -0.03


def test_ersparnis_zeile_zaehlt_nicht_mit() -> None:
    """„Sie sparen" ist eine Auskunft, kein Einkauf.

    Mitgezählt wäre der Bon um genau diesen Betrag zu teuer und die Probe fiele
    durch, obwohl jede echte Position stimmt. Die Zeile fliegt raus — sonst
    stünde im Preisverlauf ein „Artikel" namens „Sie sparen".
    """
    items, probe = pruefe_positionen(
        _pos(("Nussbrot", "4.20"), ("Hafermilch", "2.10"), ("Sie sparen", "1.50")),
        Decimal("6.30"),
    )
    assert probe.ok
    assert [it["name"] for it in items] == ["Nussbrot", "Hafermilch"]

    # Gegenprobe zur Gegenprobe: ein GEKAUFTES „Vorteilspack" darf nicht als
    # Ersparnis-Auskunft gelten, bloss weil „Vorteil" darin vorkommt.
    behalten, _ = pruefe_positionen(_pos(("Ihr Vorteilspack 6er", "8.40")), Decimal("8.40"))
    assert [it["name"] for it in behalten] == ["Ihr Vorteilspack 6er"]


def test_ohne_total_gilt_die_liste_als_ungeprueft() -> None:
    """Kein Total heisst nicht „stimmt schon" — es heisst ungeprüft."""
    _, probe = pruefe_positionen(_pos(("Nussbrot", "4.20")), None)
    assert not probe.ok
    assert probe.abweichung is None


def test_leere_liste_ist_keine_bestandene_probe() -> None:
    _, probe = pruefe_positionen([], Decimal("6.30"))
    assert not probe.ok


def test_unlesbarer_preis_laesst_die_probe_fehlschlagen() -> None:
    """Der gefährliche Fall: die übrigen Zeilen ergeben den Total zufällig genau.

    Würde die unlesbare Position übersprungen, ginge die Probe auf — und die
    Liste sähe geprüft aus, obwohl eine Position unbekannt ist.
    """
    _, probe = pruefe_positionen(
        [{"name": "Nussbrot", "price": "6.30"}, {"name": "Hafermilch", "price": "—"}],
        Decimal("6.30"),
    )
    assert not probe.ok


# ---------------------------------------------------------------------------
# Der Weg vom OCR-Text zum Vorschlagsdialog
# ---------------------------------------------------------------------------

_BON_STIMMIG = (
    "FRISCHMARKT WOLKENSTEIN\n"
    "Nussbrot Gross            4.20\n"
    "Hafermilch 1L             2.10\n"
    "TOTAL                     6.30\n"
)

# Derselbe Bon, aber eine Position trägt einen Preis, der nicht dazu gehört —
# so sieht ein Zeilenversatz nach dem OCR aus.
_BON_VERSETZT = (
    "FRISCHMARKT WOLKENSTEIN\n"
    "Nussbrot Gross            2.10\n"
    "Hafermilch 1L             7.90\n"
    "TOTAL                     6.30\n"
)

# Tabellen-Bon (Menge | Preis | Aktion | Total): der Rabatt steckt bereits in der
# rechten Spalte, und genau die liest der Zeilen-Parser.
_BON_TABELLE = (
    "FRISCHMARKT WOLKENSTEIN\n"
    "Rispentomaten 500g   1   3.40           3.40\n"
    "Hafermilch 1L        2   2.30   -0.40   4.20\n"
    "TOTAL                                   7.60\n"
)


def test_tabellenbon_mit_rabatt_im_zeilentotal_besteht() -> None:
    """Ein Rabatt, der schon im Zeilen-Total steckt, wird nicht noch einmal
    abgezogen — sonst machte die Probe aus einem stimmigen Bon einen
    unstimmigen und der Nutzer verlöre die Aufteilung ohne Grund."""
    with SessionLocal() as db:
        got = analyze(db, OcrResult(text=_BON_TABELLE, method="text-layer",
                                    amount=Decimal("7.60"), date=date(2026, 8, 3)))
        assert [it["price"] for it in got["items"]] == ["3.40", "4.20"]
        assert got["positions_ok"] is True


def test_analyze_meldet_ungepruefte_positionen() -> None:
    with SessionLocal() as db:
        ok = analyze(db, OcrResult(text=_BON_STIMMIG, method="text-layer",
                                   amount=Decimal("6.30"), date=date(2026, 8, 1)))
        assert ok["positions_ok"] is True
        assert len(ok["items"]) == 2

        schief = analyze(db, OcrResult(text=_BON_VERSETZT, method="text-layer",
                                       amount=Decimal("6.30"), date=date(2026, 8, 1)))
        assert schief["positions_ok"] is False
        # Gezeigt werden sie trotzdem: der Nutzer soll sehen und korrigieren
        # können, was gelesen wurde.
        assert len(schief["items"]) == 2


def test_analyze_reicht_die_toleranz_an_die_oberflaeche() -> None:
    """Die Oberfläche prüft bei jeder Korrektur neu und muss dieselbe Regel
    anwenden. Eine eigene Zahl im JavaScript wären zwei Regeln.

    Bewusst gegen die Zahl geprüft und nicht gegen ``RUNDUNGS_TOLERANZ``: sonst
    wanderte jede Änderung der Konstante stillschweigend mit und der Test sagte
    nur noch, dass eine Variable sich selbst gleicht.
    """
    with SessionLocal() as db:
        got = analyze(db, OcrResult(text=_BON_STIMMIG, method="text-layer",
                                    amount=Decimal("6.30"), date=date(2026, 8, 1)))
        assert got["positions_toleranz"] == "0.02"


def test_ungepruefte_positionen_werden_weder_gelernt_noch_gespeichert() -> None:
    """Der eigentliche Schutz: was nicht aufgeht, verlässt den Dialog nicht.

    Gelernte Regeln wirken auf JEDEN weiteren Beleg — eine falsche Position
    bliebe sonst dauerhaft. Der Beleg selbst wird trotzdem gespeichert: der
    Total steht gross und allein auf dem Bon und ist verlässlich.
    """
    with SessionLocal() as db:
        cid = _first_category_id(db)
        strukt = {
            "merchant": "Wolkenstein", "merchant_key": "wolkenstein",
            "date": "2026-08-01", "amount": "6.30",
            "items": [
                {"name": "Wolkenbrot Gross", "price": "2.10", "category_id": cid},
                {"name": "Nebelmilch 1L", "price": "7.90", "category_id": cid},
            ],
        }
        res = save_receipt(db, strukt, _BON_VERSETZT, source="photo")

        assert res["positions_ok"] is False
        assert res["verworfene_positionen"] == 2
        assert not db.scalars(
            select(ReceiptItemRule).where(ReceiptItemRule.keyword.in_(("wolkenbrot", "nebelmilch")))
        ).all()
        # Der Beleg ist da — nur ohne Aufteilung.
        pend = db.get(PendingReceipt, res["pending_id"])
        assert pend is not None and pend.amount == Decimal("6.30")
        assert '"items": []' in (pend.items_json or "")

        # Der Aufrufer behält seine Liste: save_receipt arbeitet auf einer Kopie.
        assert len(strukt["items"]) == 2


def test_gepruefte_positionen_werden_gelernt_und_gespeichert() -> None:
    """Gegenstück: geht die Probe auf, ändert sich am bisherigen Verhalten nichts."""
    with SessionLocal() as db:
        cid = _first_category_id(db)
        strukt = {
            "merchant": "Wolkenstein", "merchant_key": "wolkenstein",
            "date": "2026-08-02", "amount": "6.30",
            "items": [
                {"name": "Sturmbrot Gross", "price": "4.20", "category_id": cid},
                {"name": "Nebelmilch 1L", "price": "2.10", "category_id": cid},
            ],
        }
        res = save_receipt(db, strukt, _BON_STIMMIG, source="photo")

        assert res["positions_ok"] is True
        assert res["verworfene_positionen"] == 0
        assert db.scalar(
            select(ReceiptItemRule).where(ReceiptItemRule.keyword == "sturmbrot")
        ) is not None
        ziel = (db.get(PendingReceipt, res["pending_id"]).items_json
                if res["pending_id"] else
                db.scalar(select(Attachment.parsed_items_json).where(
                    Attachment.transaction_id == res["attached_tx_id"])))
        assert "Sturmbrot Gross" in (ziel or "")
