"""Tests für die Quittungs-Text-Extraktion (Text-Layer + OCR-Fallback).

Der Text-Layer-Pfad (PyMuPDF) wird voll getestet. Der Tesseract-OCR-Fallback
wird übersprungen, wenn Tesseract nicht installiert ist (lokal auf Windows).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.config import settings
from moneten.db.models import Account, AccountType, Attachment, Transaction
from moneten.db.session import SessionLocal
from moneten.services.receipt_ocr import (
    extract_amount,
    extract_date,
    extract_text,
    tesseract_available,
)


def _make_text_pdf(path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_extract_amount_variants() -> None:
    assert extract_amount("Total CHF 78.40") == Decimal("78.40")
    assert extract_amount("Summe 1'234.50") == Decimal("1234.50")
    assert extract_amount("Betrag 42,90") == Decimal("42.90")
    # Ohne Total: grösster Betrag als Heuristik
    assert extract_amount("Brot 3.50\nMilch 2.20\nKäse 12.00") == Decimal("12.00")
    assert extract_amount("kein betrag hier") is None


def test_extract_amount_grand_total_not_savings() -> None:
    """Supermarkt-Bon: „Total CHF X" gewinnt; „Sie sparen total" (Ersparnis) und
    „Zwischentotal" werden NICHT als Betrag gelesen — genau der Bug: gelesen wurde
    die Ersparnis statt der Summe, deshalb fand das Matching keine Buchung.

    Die Zahlen sind erfunden, ihre Verhältnisse nicht: Zwischentotal knapp über
    dem Total (Rundung), Ersparnis deutlich darunter."""
    text = (
        "Artikelbezeichnung Menge Preis Gespart Total\n"
        "Haferdrink 1 1.75 1.75\n"
        "Zwischentotal                  88.42\n"
        "Sie sparen total                9.30\n"
        "Total CHF                      88.40\n"
        "Total EFT CHF:                 88.40\n"
        "Kundenkarte-Nr. 0000.000.000.000\n"
    )
    assert extract_amount(text) == Decimal("88.40")


def test_extract_amount_eft_and_card_priority() -> None:
    """Der von der BANK belastete Betrag gewinnt: „Total EFT CHF" (Teilzahlung) bzw. die
    „Visa CHF"-Zeile (wenn die Summe per OCR verfälscht ist); UID-/Steuernummern raus."""
    # Teilzahlung Geschenkkarte + Karte → Bank sieht nur die 1.45 (EFT), nicht 25.60.
    assert extract_amount("Total CHF 25.60\nTotal EFT CHF: 1.45") == Decimal("1.45")
    # SUMME per OCR falsch (200.05), aber Visa-Zeile korrekt; MWST-Nr. wird ignoriert.
    assert extract_amount(
        "SUMME CHE = 200.05\nVisa . CHF * 200.85\nCHE-123 456.789 MWST"
    ) == Decimal("200.85")


def test_extract_amount_invoice_total_over_largest() -> None:
    """Rechnungen: das beschriftete Rechnungs-Total gewinnt — auch wenn Label und
    Betrag durch Spalten/Zeilen getrennt sind — und schlägt grössere Referenz- und
    Seite-2-Zahlen, die der „grösste Zahl"-Fallback sonst fälschlich nähme. Genau
    das war der Bug: gewonnen hatte zweimal eine Zahl, die gar kein Betrag ist.

    Beträge erfunden, Verhältnis beibehalten: die Störzahl ist grösser als das
    Total, das gefunden werden muss."""
    # Fall A: „Gesamtbetrag (Brutto):" + Betrag auf der nächsten Zeile; die grössere
    # Referenznummer von Seite 2 darf NICHT gewinnen.
    optiker = (
        "Position A\nCHF 40.00\nCHF 40.00\nCHF 40.00\n"
        "Gesamtbetrag (Brutto):\nCHF 120.00\n"
        "00000000000000000000000\nReferenz 350.00\n"
    )
    assert extract_amount(optiker) == Decimal("120.00")
    # Fall B: „(Zahlungsbetrag)" mit vielen Spalten-Leerzeichen vor dem Betrag;
    # die grösste Zahl der Positionsliste auf Seite 2 darf nicht gewinnen.
    verwaltung = (
        "Unsere Forderung                         CHF\n"
        "Musterfirma GmbH                         75.00\n"
        "Saldo zu unseren Gunsten (Zahlungsbetrag)                        75.00\n"
        "Positionen Seite 2: 50.00 10.00 60.00 480.00\n"
    )
    assert extract_amount(verwaltung) == Decimal("75.00")


def test_extract_amount_invoice_ignores_due_date() -> None:
    """„Zu zahlen bis 30.09.2024" darf kein Datums-Fragment (30.09) als Betrag liefern;
    das echte „Rechnungsbetrag CHF 50.00" gewinnt."""
    assert extract_amount("Zu zahlen bis 30.09.2024\nRechnungsbetrag CHF 50.00\n") == Decimal("50.00")


def test_extract_amount_ignores_swiss_uid() -> None:
    """Ochsner: die UID/MWST-Nummer „CHE-000.000.000" sieht wie 151.85 aus und darf NICHT
    als Total gegriffen werden — echtes Total 24.90 (Bug: 151.85 statt 24.90)."""
    # Kein Total-Label: der „grösste Zahl"-Fallback darf nicht die UID-Ziffern nehmen.
    assert extract_amount("Artikel 24.90\nMwSt 7.70% 1.78\nCHE-000.000.000 MWST") == Decimal("24.90")
    # Auch das Format mit Leerzeichen (Coop-UID) wird entfernt.
    assert extract_amount("Pos 13.60\nCHE-116 311.105 MWST") == Decimal("13.60")


def test_extract_amount_card_line_wide_spacing() -> None:
    """Die „Visa CHF X"-Zeile gewinnt auch mit vielen Spalten-Leerzeichen zwischen
    „Visa", „CHF" und Betrag — und die UID daneben bleibt aussen vor."""
    assert extract_amount(
        "OCHSNER SPORT\nVisa            CHF          24.90\nRückgeld CHF 0.00\nCHE-000.000.000 MWST"
    ) == Decimal("24.90")


def test_aldi_total_not_item_sum() -> None:
    """ALDI-Foto-Scan: „Kartenzahlung", „Zwischensumme" und „5 Artikel" sind KEINE
    Positionen. Erkanntes Total = 12.75 (nicht 3.30+9.45+12.75 = 25.50). Realer Bug:
    der Editor zeigte 25.50, weil es die Positions-Summe statt des Totals war."""
    from moneten.services.receipt_split import parse_receipt_items

    text = (
        "ALDI SUISSE AG\n1234 Musterstadt\n\nMusterstrasse 9\n"
        "=       537053 Hot Dog Broetchen      3.30 A\n"
        "a       307499 Vegi Brat./Frank.     9.45 A      |\n"
        "Zwischensumme\n12.75                |\n. Rundung              0.00\n"
        "\\ ALDI PREIS        12.75\n|   5 Artikel\n"
        "4       Kartenzahl ung               CHF\n12.75\n"
    )
    items = parse_receipt_items(text)
    assert len(items) == 2  # nur die zwei echten Artikel
    assert all(p != Decimal("12.75") for _, p in items)  # Zahlart/Subtotal nicht als Position
    assert extract_amount(text) == Decimal("12.75")  # = das, was der Editor als TOTAL zeigt


def test_photo_scan_total_is_detected_amount_not_item_sum() -> None:
    """Foto-Scan-Datenpfad (analyze): das Editor-Total kommt aus dem ERKANNTEN Beleg-Total
    (extract_amount), nicht aus der Positions-Summe — und die Zahlart-Zeile ist keine
    Position. Schützt End-to-End vor dem ALDI-„25.50"-Bug."""
    from moneten.services.receipt_digital import analyze
    from moneten.services.receipt_ocr import OcrResult

    text = (
        "ALDI SUISSE AG\n1234 Musterstadt\n"
        "=  537053 Hot Dog Broetchen  3.30 A\n"
        "a  307499 Vegi Brat./Frank.  9.45 A\n"
        "Zwischensumme\n12.75\n. Rundung 0.00\nALDI PREIS 12.75\n5 Artikel\n"
        "Kartenzahl ung CHF\n12.75\n"
    )
    with SessionLocal() as db:
        structured = analyze(db, OcrResult(text=text, method="ocr", amount=extract_amount(text), date=None))
    assert structured["amount"] == "12.75"                       # Editor-TOTAL = erkanntes Total
    assert len(structured["items"]) == 2                         # Zahlart/Subtotal/Artikel raus
    assert all(it["price"] != "12.75" for it in structured["items"])


def test_photo_match_uses_merchant_disambiguation() -> None:
    """Foto-Zuordnung nutzt jetzt — wie Ordner-Belege — denselben Matcher (find_match) und
    löst über den Händler auf, wenn mehrere Buchungen Betrag+Datum teilen. Vorher (nur
    Tier 1, kein Händler) gab es in dem Fall gar keinen Treffer."""
    from moneten.services.receipt_digital import _try_match
    from moneten.services.receipt_match import _merchant_tokens

    with SessionLocal() as db:
        acc = Account(name="Foto-Match-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=862)
        db.add(acc)
        db.flush()
        a = Transaction(account_id=acc.id, date=date(2026, 5, 4), amount=Decimal("-44.44"),
                        description="MIGROS MM MUSTERSTADT")
        b = Transaction(account_id=acc.id, date=date(2026, 5, 4), amount=Decimal("-44.44"),
                        description="COOP BASEL")
        db.add_all([a, b])
        db.commit()
        a_id = a.id
        tokens = _merchant_tokens("Migros", "Migros\nTotal CHF 44.44")
        tx = _try_match(db, Decimal("44.44"), date(2026, 5, 4), merchant_tokens=tokens)
        assert tx is not None and tx.id == a_id  # Migros gewinnt über den Händler im Banktext


def test_extract_amount_kartenzahlung_and_date_strip() -> None:
    """ALDI-Foto: „Kartenzahlung CHF X" gewinnt als per Karte abgebuchter Betrag; ein Datum
    „31.12.26 10:15" wird NICHT als Betrag 31.12 gelesen (gemessener Fehler: Total 31.12)."""
    text = (
        "ALDI SUISSE AG\n537053 Hot Dog 3.30 A\nALDI PREIS 12.75\n"
        "= Kartenzahlung      CHF      12.75      ee\n4 E000/000/000 31.12.26 10:15\n"
    )
    assert extract_amount(text) == Decimal("12.75")
    # Ohne Total-Zeile darf das Datum nicht gewinnen — der Fallback strippt Datum/Zeit.
    assert extract_amount("Beleg\nPos 3.30\nDatum 31.12.26 10:15\n") == Decimal("3.30")


def test_guess_merchant_known_brands() -> None:
    """Bekannte CH-Händler werden auch in verrauschten/breit gesetzten Kopfzeilen erkannt —
    und „sparen" wird NICHT als „Spar" missverstanden."""
    from moneten.services.receipt_digital import guess_merchant

    assert guess_merchant("Ä          ALDI SUISSE AG          fe\n1234 Musterstadt") == "Aldi Suisse"
    assert guess_merchant("MIGROS MM\nMusterstadt") == "Migros"
    assert guess_merchant("Galaxus AG\nZuerich") == "Galaxus"
    assert guess_merchant("Sie sparen total 5.00") is None  # „sparen" ≠ „Spar"


# Die Vorlagen sind ERFUNDEN. Sie waren einmal der Rohtext echter Belege — mit
# Filiale, Datum, Uhrzeit, Kassennummer und dem tatsaechlichen Einkauf, also
# einem Einkaufsprofil. Behalten wurde alles, wofuer die Tests da sind: die
# Haendlernamen (die sind eine Funktion, `guess_merchant` erkennt sie), das
# OCR-Rauschen, Artikelnummern, Mengenzeilen und die Summen-Schreibweisen der
# jeweiligen Kette.
_GOLDEN_RECEIPTS = [
    # (Fixture-Slug, Händler, Total, Datum-ISO|None)
    ("aldi", "Aldi Suisse", "8.15", None),      # verrauschte Fassung ohne lesbares Datum
    ("coop", "Coop", "7.90", "2026-03-12"),
    ("karma", "Karma", "9.90", None),           # „Karmd"/„Karna" → Karma (Fuzzy, 1-Zeichen-Fehler)
    ("obi", "OBI", "15.65", "2026-03-12"),
]


@pytest.mark.parametrize(("slug", "merchant", "total", "date_iso"), _GOLDEN_RECEIPTS)
def test_golden_real_phone_receipts(slug, merchant, total, date_iso) -> None:
    """Golden Tests am verrauschten OCR-Text: er liegt als Fixture fest → Händler + Total MÜSSEN stimmen, und das Datum, wo die OCR es lesbar
    erfasst hat. Nagelt die 4 Bons dauerhaft fest, damit genau diese Fälle nie wieder
    still brechen."""
    from moneten.services.receipt_digital import guess_merchant
    from moneten.services.receipt_split import _looks_like_code, parse_receipt_items

    text = (Path(__file__).parent / "fixtures" / f"ocr_{slug}.txt").read_text(encoding="utf-8")
    assert guess_merchant(text) == merchant
    assert extract_amount(text) == Decimal(total)
    if date_iso:
        got = extract_date(text)
        assert got is not None and got.isoformat() == date_iso
    # Keine Müll-Positionen: keine Referenz/Barcode-Codes, keine Footer-Zeilen, und kein
    # Posten-Preis = das Beleg-Total (das wäre eine fälschlich als Position gelesene Summe).
    items = parse_receipt_items(text)
    assert all(not _looks_like_code(n) for n, _ in items)
    assert not any("genossenschaft" in n.lower() for n, _ in items)
    assert all(p != Decimal(total) for _, p in items)


_GOLDEN_RAPIDOCR = [
    # (Slug, Händler, Total, Datum-ISO|None, bereinigte Positionsnamen) — die
    # layout-rekonstruierte Fassung derselben erfundenen Belege.
    ("aldi", "Aldi Suisse", "8.15", "2026-03-12", ["Mineral Still 1.5L", "Joghurt Natur 180G"]),
    ("coop", "Coop", "7.90", "2026-03-12",
     ["Ruchbrot 400G", "Apfel Gala 1KG", "Mineral 50CL"]),
    ("karma", "Karma", "9.90", None, ["Ingwer Shot 200ML", "Nussmischung 150G"]),
    ("obi", "OBI", "15.65", "2026-03-12", ["Blumenerde 20L", "Pflanzstab 120CM", "Bindedraht Gruen"]),
]


@pytest.mark.parametrize(("slug", "merchant", "total", "date_iso", "names"), _GOLDEN_RAPIDOCR)
def test_golden_rapidocr_receipts(slug, merchant, total, date_iso, names) -> None:
    """Golden Tests auf dem ECHTEN RapidOCR-Text (Layout-rekonstruiert) der 4 Handy-Bons:
    Händler + Total + Datum stimmen, und die Positionen sind **sauber** — exakt die erwarteten
    Namen (keine Artikelnummern, kein „6x"-Multipack, Volumen wie „50CL"/„250G" bleibt)."""
    from moneten.services.receipt_digital import guess_merchant
    from moneten.services.receipt_split import parse_receipt_items

    text = (Path(__file__).parent / "fixtures" / f"ocr_rapid_{slug}.txt").read_text(encoding="utf-8")
    assert guess_merchant(text) == merchant
    assert extract_amount(text) == Decimal(total)
    if date_iso:
        got = extract_date(text)
        assert got is not None and got.isoformat() == date_iso
    assert [n for n, _ in parse_receipt_items(text)] == names


def test_reading_direction_distinguishes_upside_down() -> None:
    """Die Leserichtung (Name links / Preis rechts) trennt 0° von 180° — nötig, weil
    RapidOCR auch KOPFÜBER sauberen Text liest (gleiche Betrags-/Wortanzahl). Realer Bug:
    ein 180°-Bon wurde mit gespiegelten Spalten gelesen → keine Positionen geparst."""
    from moneten.services.receipt_ocr import _reading_direction_score

    upright = "Fruchtgummi Beutel 1.95\nMineral Dose 50CL 11.95\nTOTAL CHF 13.90"
    flipped = "1.95 Fruchtgummi Beutel\n11.95 Mineral Dose 50CL\n13.90 TOTAL CHF"
    assert _reading_direction_score(upright) > _reading_direction_score(flipped)
    assert _reading_direction_score(flipped) == 0  # Preis ganz links → nichts davor


def test_parse_skips_space_split_zwischensumme() -> None:
    """RapidOCR trennt „Zwischensumme" manchmal („Zwi schensumme") — das darf NICHT als
    Position durchrutschen (der Skip prüft auch leerzeichen-zusammengezogen)."""
    from moneten.services.receipt_split import parse_receipt_items

    text = "Brotchen 3.30\nVegi Brat. 9.45\nZwi schensumme 12.75\nTOTAL CHF 12.75"
    items = parse_receipt_items(text)
    assert len(items) == 2
    assert not any("schensumme" in n.lower() for n, _ in items)


def test_parse_skips_glued_mwst_and_terminal_lines() -> None:
    """OCR klebt Zahlen direkt an Schlüsselwörter. Realer Bug, erfundene Zahlen: eine
    Steuerzeile („Umsatz8.1%ink1 MwSt8.1% CHF…") und eine Terminal-Zeile
    („Termina1CHF …", l→1) rutschten als Positionen durch, weil ``\\b`` an der
    Buchstabe→Ziffer-Grenze nicht greift. Der Glued-Skip muss beide aussortieren,
    echte Positionen bleiben."""
    from moneten.services.receipt_split import parse_receipt_items

    text = (
        "Taverne Beispiel\n"
        "Musterwein Junm a 18.00\n"
        "Beispielgin F1 a 24.00\n"
        "Umsatz8.1%ink1 MwSt8.1% CHF3.15\n"
        "Termina1CHF 42.00\n"
        "TOTAL CHF 42.00\n"
    )
    items = parse_receipt_items(text)
    names = [n.lower() for n, _ in items]
    assert len(items) == 2, items
    assert any("musterwein" in n for n in names)
    assert any("gin" in n for n in names)
    assert not any("mwst" in n or "umsatz" in n or "termina" in n for n in names)


def test_parse_migros_footer_ocr_variants() -> None:
    """Migros-Bon (Tabellen-Layout) mit drei OCR-Footer-Fallen, alle gemessen:
    „Rundungsuorteil" (v→u, und ‚rundung' ist nur Wortpräfix), „Uisa" (Visa mit
    V→u) und die Kartenmaske „XXXXXXXXXXXX0000 1107.2026" (Datum ohne Punkte →
    sah wie Preis 1107.20 aus). Alle drei dürfen KEINE Position werden; die fünf
    echten Artikel (rechteste Spalte = Total) bleiben."""
    from moneten.services.receipt_split import parse_receipt_items

    text = (
        "MIGROS\n"
        "Genossenschaft Migros Musterland\n"
        "Evian Sport 75cl 1 1.15 0.38 0.77 1\n"
        "Granatapfel 1 3.50 1.15 2.35 S1\n"
        "Sandwich Carpaccio 1 5.50 1.80 3.70 S1\n"
        "Göffel 1 0.10 0.10 2\n"
        "Paulaner Spezi 1 1.50 1.50 1\n"
        "Rundungsuorteil 0.02-\n"
        "Sie sparen total 3.35\n"
        "Total CHF 8.40\n"
        "Uisa 8.40\n"
        "Total in EUR 9.77\n"
        "*** Kundenbeleg ***\n"
        "XXXXXXXXXXXX0000 1107.2026\n"
    )
    items = parse_receipt_items(text)
    names = [n.lower() for n, _ in items]
    prices = [p for _, p in items]
    assert len(items) == 5, items
    assert not any("rundung" in n or "uisa" in n or "visa" in n or "xxx" in n for n in names)
    assert Decimal("0.77") in prices and Decimal("3.70") in prices  # Total-Spalte, nicht Preis-Spalte
    assert Decimal("1107.20") not in prices

    from moneten.services.receipt_ocr import extract_amount
    assert extract_amount(text) == Decimal("8.40")


def test_orientation_score_ignores_garbage_length() -> None:
    """Der Orientierungs-Score zählt Beträge + echte Wörter, NICHT die Textlänge — sonst
    würde langer gedrehter Zeichen-Müll eine korrekte (kürzere) Lesung schlagen (genau der
    Bug, der die Auto-Drehung erst lahmlegte)."""
    from moneten.services.receipt_ocr import _orientation_score

    good = "MIGROS MM\nBrot 3.50\nMilch 2.20\nTotal CHF 12.75\nVisa CHF 12.75"
    garbage = "|/\\_.-= " * 500  # sehr lang, aber 0 Beträge / 0 Wörter
    assert _orientation_score(good) > _orientation_score(garbage)


# ------------------------------------------------------- Zeilenbildung aus OCR-Boxen
#
# Die Belege hier sind ERFUNDEN: echte Layouts, erfundene Werte. Geprüft wird gegen
# ``_layout_text`` direkt — mit selbst gebauten (Box, Text, Confidence)-Tripeln, ohne
# Bild und ohne OCR-Engine. Die Geometrie stammt aus einer Messung an echten RapidOCR-
# Ausgaben (gerenderter Bon, gedreht): die Boxen sind Vierecke im Uhrzeigersinn ab
# links oben, ihre Oberkante trägt die Neigung der Textzeile (gemessen 0.060 bei 3.5°,
# erwartet tan 3.5° = 0.061), und die Seitenkante bleibt dabei die Zeilenhöhe.


def _fetzen(zeilen, *, oben=40.0, schritt=30.0, hoehe=23.0, neigung=0.0, woelbung=0.0):
    """(Box, Text, Confidence)-Tripel eines erfundenen Belegs.

    ``zeilen`` ist je Bon-Zeile eine Liste von ``(x, Text)``-Spalten. Zwei Arten, wie
    ein Handy-Foto die Zeilen verzieht, sind getrennt einstellbar: ``neigung`` kippt den
    ganzen Beleg (schräg gehaltenes Handy), ``woelbung`` zieht nur die rechte Hälfte
    nach unten (gewölbtes Thermopapier — links liegt der Bon flach auf).
    """
    fetzen = []
    for i, spalten in enumerate(zeilen):
        for j, (x, text) in enumerate(spalten):
            breite = 13.0 * len(text)
            y = oben + i * schritt + (((i * 7 + j * 3) % 3) - 1.0)  # ±1 px Rundung wie echt
            if x > 250:
                y += woelbung
            ecken = [(x, y), (x + breite, y), (x + breite, y + hoehe), (x, y + hoehe)]
            fetzen.append(([[ex, ey + neigung * ex] for ex, ey in ecken], text, 0.97))
    return list(reversed(fetzen))  # die Engine liefert die Fetzen unsortiert


#: Supermarkt-Bon mit den Spalten Menge | Preis | Rabatt | Total, eng gesetzt.
#: Eine Zeile mit Menge 2, zwei Zeilen mit Rabatt. Summe der Total-Spalte = 20.20.
_BON_SUPERMARKT = [
    [(20, "SUPERMARKT SEEBLICK")],
    [(20, "Bahnhofplatz 4")],
    [(20, "Artikel"), (300, "Menge"), (380, "Preis"), (470, "Rabatt"), (560, "Total")],
    [(20, "Haferflocken 500G"), (310, "1"), (380, "2.90"), (560, "2.90")],
    [(20, "Zitronenwasser 50CL"), (310, "2"), (380, "1.60"), (560, "3.20")],
    [(20, "Waschmittel 1L"), (310, "1"), (380, "9.80"), (470, "2.00"), (560, "7.80")],
    [(20, "Nussgipfel"), (310, "1"), (380, "2.20"), (470, "0.50"), (560, "1.70")],
    [(20, "Bio Eier 6er"), (310, "1"), (380, "4.60"), (560, "4.60")],
    [(20, "TOTAL CHF"), (560, "20.20")],
]

_BON_SUPERMARKT_ZEILEN = [
    "SUPERMARKT SEEBLICK",
    "Bahnhofplatz 4",
    "Artikel Menge Preis Rabatt Total",
    "Haferflocken 500G 1 2.90 2.90",
    "Zitronenwasser 50CL 2 1.60 3.20",
    "Waschmittel 1L 1 9.80 2.00 7.80",
    "Nussgipfel 1 2.20 0.50 1.70",
    "Bio Eier 6er 1 4.60 4.60",
    "TOTAL CHF 20.20",
]


def test_box_geometrie_misst_die_zeile_statt_der_ausdehnung() -> None:
    """Eine geneigte Box meldet ihre ZEILENHÖHE (Seitenkante) und ihre Neigung.

    Die achsenparallele Ausdehnung taugt als Zeilenhöhe nicht: sie wächst mit der
    Breite der Box. An echten RapidOCR-Ausgaben nachgemessen meldete eine 286 px breite
    Namens-Box bei 3.5° Neigung 40 px statt 23 — und über den Median blähte das die
    Zeilen-Toleranz auf, bis die nächste Bon-Zeile hineinfiel.
    """
    from moneten.services.receipt_ocr import _box_geometrie

    hoehe, breite, neigung = 23.0, 260.0, 0.06
    ecken = [(20.0, 100.0), (20.0 + breite, 100.0),
             (20.0 + breite, 100.0 + hoehe), (20.0, 100.0 + hoehe)]
    box = [[x, y + neigung * x] for x, y in ecken]

    _y, _x, links, gemessen, steigung = _box_geometrie(box)
    assert gemessen == pytest.approx(hoehe, abs=0.5)
    assert steigung == pytest.approx(neigung, abs=0.005)
    assert links == pytest.approx(20.0)
    # Die Falle, gegen die gemessen wird: die Ausdehnung ist hier fast doppelt so gross.
    ausdehnung = max(p[1] for p in box) - min(p[1] for p in box)
    assert ausdehnung > hoehe * 1.6


def test_layout_zieht_schraeg_fotografierten_beleg_gerade() -> None:
    """Schräg fotografierter Bon (3.4°): die Zeilen kommen einzeln heraus.

    Ohne Geradeziehen läuft die y-Mitte einer Zeile von links nach rechts weg — über
    die Belegbreite hier um 32 px bei 30 px Zeilenabstand. Damit steht ein Preis tiefer
    als der Name der nächsten Zeile, und die Zeilen verzahnen sich.
    """
    from moneten.services.receipt_ocr import _layout_text

    text = _layout_text(_fetzen(_BON_SUPERMARKT, neigung=0.06))
    assert text.splitlines() == _BON_SUPERMARKT_ZEILEN


def test_layout_verschmilzt_gewoelbten_bon_nicht() -> None:
    """Gewölbter Bon (rechte Hälfte 11 px tiefer), eng gesetzt: keine zwei Bon-Zeilen
    in einer Ausgabezeile.

    Genau hier zog der frühere laufende Mittelwert die Zeile nach unten: die vier
    Zahlenspalten liegen dicht beieinander und tiefer als der Name, der Mittelwert
    rutschte ihnen nach, und die nächste Bon-Zeile fiel dadurch noch in die Toleranz —
    zwei Produktnamen in einer Position, danach alles um eine Zeile versetzt.
    """
    from moneten.services.receipt_ocr import _layout_text

    zeilen = _layout_text(_fetzen(_BON_SUPERMARKT[3:6], schritt=20, woelbung=11)).splitlines()
    assert zeilen == _BON_SUPERMARKT_ZEILEN[3:6]


def test_layout_grosser_kopf_hebt_die_zeilentoleranz_nicht() -> None:
    """Ein gross gesetzter Beleg-Kopf darf die Zeilen-Toleranz des Fliesstexts nicht heben.

    Auf einem kurzen Bon stellt der Kopf schnell die Hälfte aller Fetzen — dann liegt der
    MEDIAN der Höhen bei der Kopfzeile (hier 52 px statt 22), und mit ihm wird die
    Toleranz grösser als der Zeilenabstand des Fliesstexts (26 px): die beiden Positionen
    fielen in eine Zeile. Das untere Quartil beschreibt weiter den Fliesstext.
    """
    from moneten.services.receipt_ocr import _layout_text

    kopf = _fetzen([[(20, "KIOSK"), (300, "SEEBLICK")], [(20, "Bahnhofplatz"), (300, "4")]],
                   oben=40, schritt=64, hoehe=52)
    rumpf = _fetzen([[(20, "Kaugummi Minze"), (560, "2.40")], [(20, "Mineral 50CL"), (560, "3.10")]],
                    oben=200, schritt=26, hoehe=22)
    zeilen = _layout_text(kopf + rumpf).splitlines()
    assert zeilen == ["KIOSK SEEBLICK", "Bahnhofplatz 4",
                      "Kaugummi Minze 2.40", "Mineral 50CL 3.10"]


def test_positionen_aus_schraegem_supermarkt_bon() -> None:
    """Ganze Kette am schrägen Supermarkt-Bon: Boxen → Zeilen → Positionen.

    Die Mengenspalte gilt erst, wenn die Zeile ihre eigene Rechnung bestätigt
    (2 × 1.60 = 3.20) — dann ist der Preis das ZEILEN-TOTAL und nicht der Einzelpreis,
    und die Stückzahl steht für den Preisverlauf daneben. Die Summe der Positionen
    trifft das Beleg-Total; Kopf, Spaltenüberschrift und Summenzeile sind keine
    Positionen.
    """
    from moneten.services.receipt_ocr import _layout_text
    from moneten.services.receipt_split import parse_receipt_items_menge

    items = parse_receipt_items_menge(_layout_text(_fetzen(_BON_SUPERMARKT, neigung=0.06)))
    assert items == [
        ("Haferflocken 500G", Decimal("2.90"), 1),
        ("Zitronenwasser 50CL", Decimal("3.20"), 2),
        ("Waschmittel 1L", Decimal("7.80"), 1),
        ("Nussgipfel", Decimal("1.70"), 1),
        ("Bio Eier 6er", Decimal("4.60"), 1),
    ]
    assert sum(p for _, p, _ in items) == Decimal("20.20")


# Erfundener Kinobeleg: rechts vom Preis steht eine MwSt-Spalte, und der erste
# Positionsname läuft über drei Zeilen. Beides zusammen lieferte vorher gar keine
# Positionen — die Steuerspalte machte jede Zeile zur „Summenzeile", und ihr rechtestes
# Geld-Token war der Steuersatz.
_BON_KINO = """\
STADTKINO SEEBLICK
Saal 3
Grosse Nachoschale mit
Kaesedip und extra
Jalapenos
        8.90 CHF   MwSt 2.60 %
Popcorn salzig
        7.50 CHF   MwSt 2.60 %
Mineral still 50CL
        3.50 CHF   MwSt 2.60 %
Total                19.90 CHF
"""


def test_kinobeleg_mit_mwst_spalte_liefert_ganze_positionen() -> None:
    """Kinobeleg: drei Positionen, Preis statt Steuersatz, Name über drei Zeilen.

    Drei Dinge müssen dafür stimmen: die Zahl vor dem Prozentzeichen ist kein Betrag;
    „CHF"/„MwSt" RECHTS vom Preis sind Spaltenköpfe und keine Summenzeilen-Marke; und
    der über mehrere Zeilen laufende Name wird zusammengeführt statt auf die letzte
    Zeile verkürzt.
    """
    from moneten.services.receipt_split import parse_receipt_items

    assert parse_receipt_items(_BON_KINO) == [
        ("Grosse Nachoschale mit Kaesedip und extra Jalapenos", Decimal("8.90")),
        ("Popcorn salzig", Decimal("7.50")),
        ("Mineral still 50CL", Decimal("3.50")),
    ]


def test_einheit_am_summenwort_klebend_ist_keine_position() -> None:
    """„TotalCHF 20.20" (OCR ohne Leerzeichen) ist eine Summenzeile, „Milchflasche" nicht.

    Die Einheit klebt auf Fotos regelmässig am Wort davor; erkannt wird sie am Wechsel
    klein→GROSS. Ohne diese Einschränkung risse die Zeichenfolge „chf" mitten in
    „Milchflasche" eine echte Position weg — in Grossschreibung („MILCHFLASCHE") wäre
    sie nicht von der geklebten Einheit zu unterscheiden und muss deshalb bleiben.
    """
    from moneten.services.receipt_split import parse_receipt_items

    assert parse_receipt_items("Nussgipfel 1.70\nTotalCHF 20.20\n") == [
        ("Nussgipfel", Decimal("1.70")),
    ]
    assert parse_receipt_items("Nussgipfel 1.70\nUmsatzMwSt 8.10\n") == [
        ("Nussgipfel", Decimal("1.70")),
    ]
    assert parse_receipt_items("Milchflasche 1L 2.40\n") == [("Milchflasche 1L", Decimal("2.40"))]
    assert parse_receipt_items("MILCHFLASCHE 2.40\n") == [("MILCHFLASCHE", Decimal("2.40"))]


def test_extract_amount_ignoriert_steuersatz() -> None:
    """Der Steuersatz ist kein Betrag — auch dann nicht, wenn er die grösste Zahl ist.

    Auf einem kleinen Beleg („Espresso 4.50 CHF  MwSt 8.10 %") übertrifft der Satz den
    Preis, und der „grösste Zahl"-Fallback machte ihn zum Beleg-Total.
    """
    assert extract_amount("STADTKINO\nEspresso 4.50 CHF   MwSt 8.10 %\n") == Decimal("4.50")


def test_ocr_auto_rotates_sideways_image() -> None:
    """End-to-End: ein um 90° gedreht „hochgeladenes" Beleg-Bild wird automatisch
    geradegerückt und der Betrag korrekt gelesen (dreh-unabhängige OCR)."""
    if not tesseract_available():
        pytest.skip("Tesseract nicht installiert")
    from PIL import Image, ImageDraw, ImageFont

    from moneten.services.receipt_ocr import _ocr_pil_image

    img = Image.new("RGB", (760, 480), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    for i, line in enumerate(["MIGROS MM", "Brot      3.50", "Milch     2.20",
                              "Kaese     7.05", "TOTAL CHF    12.75"]):
        draw.text((40, 40 + i * 78), line, fill="black", font=font)
    sideways = img.rotate(90, expand=True)  # quer „fotografiert/hochgeladen"
    assert extract_amount(_ocr_pil_image(sideways)) == Decimal("12.75")


def test_extract_date_comma_separator_and_label() -> None:
    """OCR-Datum mit Komma-Trenner („28,11.2024") und „Zeit:"-Label wird erkannt."""
    assert extract_date("satum:28,11.2024 Zeit:18:56:35 Bon: 101") == date(2024, 11, 28)


def test_extract_date_prefers_transaction_timestamp() -> None:
    """Mehrere Daten auf dem Beleg → das Datum MIT Uhrzeit (Transaktions-Zeitstempel)
    gewinnt; „Punktestand per …" und Aktions-/Gültig-bis-Daten (ohne Uhrzeit) werden
    NICHT genommen."""
    text = (
        "GENOSSENSCHAFT MIGROS MUSTERLAND\n"
        "Punktestand per 30.09.2023\n"
        "Aktion gültig bis 22.05.2025\n"
        "Filiale Bedien. KN Bon Datum Zeit\n"
        "0074330 0537999 255 0063 04.10.2023 10:45:33\n"
    )
    assert extract_date(text) == date(2023, 10, 4)


def test_extract_text_text_layer(tmp_path) -> None:
    p = tmp_path / "bon.pdf"
    _make_text_pdf(p, "Migros Musterstadt\nLebensmittel 60.00\nTotal CHF 78.40")
    res = extract_text(str(p))
    assert res.method == "text-layer"
    assert "Migros" in res.text
    assert res.amount == Decimal("78.40")


def test_extract_text_image_without_tesseract(tmp_path) -> None:
    """Bild ohne Tesseract → graceful (method 'none', kein Text)."""
    img = tmp_path / "foto.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # kein echtes Bild, aber Endung zählt
    if tesseract_available():
        pytest.skip("Tesseract installiert — Bild-OCR-Pfad wird separat abgedeckt")
    res = extract_text(str(img))
    assert res.method == "none"
    assert res.text == ""


def test_ocr_runs_on_assign(logged_in_client: TestClient, tmp_path) -> None:
    """Beim Zuordnen einer Quittung aus dem Ordner wird der Text ausgelesen."""
    receipt = tmp_path / "20260515_Migros.pdf"
    _make_text_pdf(receipt, "Migros Musterstadt\nTotal CHF 78.40")

    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            acc = Account(name="OCR-Konto", type=AccountType.BANK, currency="CHF",
                          opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=800)
            db.add(acc)
            db.flush()
            tx = Transaction(account_id=acc.id, date=date.today(), amount=Decimal("-78.40"), description="Migros")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        resp = logged_in_client.post(f"/transactions/{tx_id}/attachment",
                                     data={"filename": "20260515_Migros.pdf"})
        assert resp.status_code == 200

        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.transaction_id == tx_id))
            assert att.ocr_text is not None
            assert "Migros" in att.ocr_text
            meta = json.loads(att.parsed_items_json)
            assert meta["method"] == "text-layer"
            assert meta["amount"] == "78.40"
        # Vorschau erscheint im Edit-Formular
        assert "receipt-preview" in resp.text
    finally:
        settings.receipts_dir = old


# Baumarkt-Beleg als OCR-Text, nachgebaut nach einem gemessenen Fall: mehrzeilige
# Positionen (Artikelname in
# einer Zeile, „1 ST … Preis" in der nächsten) + viel Kopf-/Fuss-Metadaten. Genau der
# Fall, der vorher Müll lieferte (Öffnungszeit als Position mit 19.00, Total 47.20
# statt 33.95, Händler „Bon-ID").
_OBI_TEXT = """\
Bon-ID
0000000000000000 0000
OBI Schweiz GmbH
Markt Nr.001; Musterstadt
Musterstrasse 40
1234 Musterstadt
Telefon: 000 000 0000
eMail: markt@example.org
Öffnungszeit: Mo-Do 8:00-19.00 Uhr
Fr 8:00-21.00 Uhr Sa 8:00-17:00 Uhr
Lieferdatum = Kassenbondatum
00001 20 0000  0000  31.12.2026 10:15:00
7610000000001  Gründüngung
1 ST                A      5.75
4000000000002  Jutegarn Grün
1 ST                B      4.50
4000000000003  Bambusstab
6 ST  a  3.95  B           23.70
Endsumme in SFr             33.95
visa                        33.95
Datum                       31.12.2026
"""


def test_parse_obi_receipt_skips_metadata_and_reads_total() -> None:
    """OBI-Beleg: keine Metadaten als Position, Total aus „Endsumme", mehrzeilige
    Positionen mit Namen aus der Vorzeile, Händler korrekt."""
    from moneten.services.receipt_digital import guess_merchant
    from moneten.services.receipt_split import parse_receipt_items

    items = parse_receipt_items(_OBI_TEXT)
    names = [n.lower() for n, _ in items]
    prices = [p for _, p in items]

    # Händler korrekt (nicht „Bon-ID").
    assert "obi" in (guess_merchant(_OBI_TEXT) or "").lower()
    # Endsumme korrekt aus „Endsumme in SFr 33.95" (nicht 47.20 = Summe falscher Items).
    assert extract_amount(_OBI_TEXT) == Decimal("33.95")
    # Genau drei Positionen mit den richtigen Preisen; Namen aus der Vorzeile.
    assert prices == [Decimal("5.75"), Decimal("4.50"), Decimal("23.70")]
    assert any("gründüngung" in n for n in names)
    assert any("jutegarn" in n for n in names)
    assert any("bambusstab" in n for n in names)
    # KEINE Kopf-/Fuss-Metadaten als Position.
    assert not any(
        any(bad in n for bad in ("öffnung", "bon", "uhr", "telefon", "endsumme", "visa"))
        for n in names
    )
    # Positions-Summe == Beleg-Total (vorher fälschlich 47.20).
    assert sum(prices) == Decimal("33.95")


def test_preprocess_pipeline_runs_on_image() -> None:
    """Die Bild-Vorverarbeitung (Hochskalieren, Entrauschen, adaptive Binarisierung)
    läuft fehlerfrei und liefert ein gültiges, hochskaliertes Graustufen-/SW-Bild —
    auch ohne Tesseract (rein PIL)."""
    from PIL import Image, ImageDraw

    from moneten.services.receipt_ocr import _preprocess

    img = Image.new("RGB", (320, 520), "white")
    d = ImageDraw.Draw(img)
    d.text((16, 16), "OBI Schweiz GmbH\nGruenduengung   5.75\nEndsumme   33.95", fill="black")
    # Beide Varianten (mit/ohne adaptive Binarisierung) laufen fehlerfrei durch.
    for binarize in (True, False):
        out = _preprocess(img, binarize=binarize)
        assert out is not None
        assert out.mode in ("L", "1")
        assert 2400 <= max(out.size) <= 2800  # in den Zielbereich normalisiert


# Migros-Beleg als OCR-Text, nachgebaut nach einem gemessenen Fall: TABELLE mit Spalten
# Artikel | Menge | Preis | Aktion | Total und OCR-Müll am Zeilenende („0", „|").
# Genau der Fall, der vorher 0 Positionen lieferte (Preis stand nicht am Zeilenende).
_MIGROS_TEXT = """\
Für mich und dich.

Filiale Musterstadt

Artikel          Menge Preis Aktion      Total
Mischbrot 2506           1 2.50              2.40 0
Konfitüre 4506          I 1.20            1.20 0
Eistee 33CL             2 19          3.80 0
Mineral Dose 506)       2 2.10            4.20 |
TOTAL CHF          11.74
Visa Debit                    |       11.7
"""


def test_parse_migros_table_receipt() -> None:
    """Migros-Tabellen-Beleg: rechteste Spalte (Total) je Position, OCR-Müll am
    Zeilenende ignoriert, Kopf/Summe/Zahlart nicht als Position, Total korrekt."""
    from moneten.services.receipt_split import parse_receipt_items

    items = parse_receipt_items(_MIGROS_TEXT)
    names = [n.lower() for n, _ in items]
    prices = [p for _, p in items]

    assert prices == [Decimal("2.40"), Decimal("1.20"), Decimal("3.80"), Decimal("4.20")]
    assert any("mischbrot" in n for n in names)
    assert any("konfitüre" in n for n in names)
    assert any("eistee" in n for n in names)
    assert any("mineral" in n for n in names)
    # Total aus „TOTAL CHF 11.74" (nicht aus Positions-/Header-Zeilen).
    assert extract_amount(_MIGROS_TEXT) == Decimal("11.74")
    # KEINE Kopf-/Summen-/Zahlart-Zeile als Position.
    assert not any(
        any(b in n for b in ("total", "visa", "artikel", "menge", "filiale")) for n in names
    )


def test_ocr_diagnostics_never_crashes() -> None:
    """Die OCR-Diagnose läuft immer durch (auch ohne Tesseract) und liefert einen
    nützlichen Block — sie landet im „OCR-Rohtext", wenn nichts erkannt wurde."""
    from PIL import Image

    from moneten.services.receipt_ocr import _ocr_diagnostics

    diag = _ocr_diagnostics(Image.new("RGB", (300, 400), "white"))
    assert isinstance(diag, str)
    assert "OCR-Diagnose" in diag
    assert "tesseract im PATH" in diag


def test_parse_date_ddmmyyyy_no_separator() -> None:
    """Alte Dateinamen wie 07012024 (TTMMJJJJ ohne Trenner) werden als Datum erkannt;
    YYYYMMDD behält Vorrang."""
    from moneten.services.attachments import parse_date_from_name

    assert parse_date_from_name("07012024_Beleg.pdf") == date(2024, 1, 7)
    assert parse_date_from_name("13082024_Manor.pdf") == date(2024, 8, 13)
    assert parse_date_from_name("20240518_Coop.pdf") == date(2024, 5, 18)  # YYYYMMDD bleibt korrekt


def test_extract_text_uses_cache(tmp_path) -> None:
    """extract_text cacht je Datei: zweiter Aufruf liefert dasselbe Objekt (kein Neu-OCR)."""
    from moneten.services.receipt_ocr import _OCR_CACHE, extract_text

    p = tmp_path / "beleg.pdf"
    _make_text_pdf(p, "Migros Musterstadt\nTotal CHF 9.90")
    _OCR_CACHE.clear()
    r1 = extract_text(str(p))
    r2 = extract_text(str(p))
    assert r1 is r2  # zweiter Aufruf kommt aus dem Cache
    assert r1.amount == Decimal("9.90")


def test_serve_receipt_file_inline_and_traversal(logged_in_client: TestClient, tmp_path) -> None:
    """Beleg-Datei wird inline ausgeliefert; unbekannte Datei / Traversal → 404."""
    (tmp_path / "20240518_Coop.pdf").write_bytes(b"%PDF-1.4\n%dummy\n")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        ok = logged_in_client.get("/import/receipts/file", params={"name": "20240518_Coop.pdf"})
        assert ok.status_code == 200
        assert ok.content[:4] == b"%PDF"
        assert logged_in_client.get("/import/receipts/file", params={"name": "gibtsnicht.pdf"}).status_code == 404
        assert logged_in_client.get("/import/receipts/file", params={"name": "../../etc/passwd"}).status_code == 404
    finally:
        settings.receipts_dir = old


def test_diagnose_receipt_file(tmp_path) -> None:
    """Die Datei-OCR-Diagnose läuft durch (auch ohne Tesseract) und meldet fehlende Dateien."""
    from moneten.services.receipt_ocr import diagnose_receipt_file

    pdf = tmp_path / "x.pdf"
    _make_text_pdf(pdf, "Migros Musterstadt\nTotal CHF 9.90")
    d = diagnose_receipt_file(str(pdf))
    assert "OCR-Diagnose" in d and "Text-Layer" in d
    assert "nicht gefunden" in diagnose_receipt_file(str(tmp_path / "fehlt.pdf"))


def test_auto_match_uses_merchant_to_disambiguate(tmp_path) -> None:
    """Zwei gleich-teure Buchungen im Datumsfenster → der Auto-Abgleich ordnet über den
    Händler aus dem Dateinamen („Migros") der richtigen Buchung zu, nicht der Coop-Buchung.
    Prüft zugleich das 7-Tage-Fenster (Migros-Buchung 2 Tage nach Beleg-Datum)."""
    from moneten.services.attachments import ReceiptFile
    from moneten.services.receipt_match import auto_match_one

    receipt = tmp_path / "20240518_Migros_001.pdf"
    _make_text_pdf(receipt, "Migros Musterstadt\nTotal CHF 12.50")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            acc = Account(name="Match-Konto", type=AccountType.BANK, currency="CHF",
                          opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=850)
            db.add(acc)
            db.flush()
            t_coop = Transaction(account_id=acc.id, date=date(2024, 5, 18),
                                 amount=Decimal("-12.50"), description="COOP Musterstadt")
            t_migros = Transaction(account_id=acc.id, date=date(2024, 5, 20),
                                   amount=Decimal("-12.50"), description="MIGROS MM")
            db.add_all([t_coop, t_migros])
            db.commit()
            mig_id = t_migros.id

            rf = ReceiptFile(name=receipt.name, path=str(receipt),
                             parsed_date=date(2024, 5, 18), size=receipt.stat().st_size)
            assert auto_match_one(db, rf) is True
            att = db.scalar(select(Attachment).where(Attachment.original_name == receipt.name))
            assert att is not None and att.transaction_id == mig_id  # Migros, nicht Coop
    finally:
        settings.receipts_dir = old


def test_bulk_archive_unmatchable(logged_in_client: TestClient, tmp_path) -> None:
    """Sammel-Archivierung legt einen Beleg ohne passende Bankbuchung ab (Datei bleibt)."""
    from moneten.db.models import ArchivedReceipt

    _make_text_pdf(tmp_path / "20240518_NurRechnung.pdf", "Rechnung\nTotal CHF 9876.54")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        r = logged_in_client.post("/import/receipts/archive-unmatchable")
        assert r.status_code == 200
        with SessionLocal() as db:
            archived = db.scalar(
                select(ArchivedReceipt).where(ArchivedReceipt.filename == "20240518_NurRechnung.pdf")
            )
            assert archived is not None
    finally:
        settings.receipts_dir = old


def test_archive_then_unarchive_restores_receipt(logged_in_client: TestClient, tmp_path) -> None:
    """Archivieren versteckt den Beleg; die Archiv-Seite zeigt ihn; Reaktivieren holt ihn
    zurück in den Assistenten (kein „stilles Verschwinden")."""
    from moneten.services.receipt_match import unassigned_receipts

    _make_text_pdf(tmp_path / "20240601_ReAktiv.pdf", "Shop\nTotal CHF 12.00")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        hx = {"HX-Request": "true"}
        logged_in_client.post("/import/receipts/archive",
                              data={"filename": "20240601_ReAktiv.pdf"}, headers=hx)
        with SessionLocal() as db:
            assert all(rf.name != "20240601_ReAktiv.pdf" for rf in unassigned_receipts(db))  # versteckt

        page = logged_in_client.get("/import/receipts/archived")
        assert page.status_code == 200 and "20240601_ReAktiv.pdf" in page.text  # im Archiv sichtbar

        u = logged_in_client.post("/import/receipts/unarchive",
                                  data={"filename": "20240601_ReAktiv.pdf"}, headers=hx)
        assert u.status_code == 200
        with SessionLocal() as db:
            assert any(rf.name == "20240601_ReAktiv.pdf" for rf in unassigned_receipts(db))  # wieder da
    finally:
        settings.receipts_dir = old


def test_receipt_assign_then_unassign(logged_in_client: TestClient, tmp_path) -> None:
    """Zuordnen erzeugt einen Anhang; „Rückgängig" (/unassign) löscht ihn wieder und
    liefert die Beleg-Karte zurück."""
    _make_text_pdf(tmp_path / "20240518_Coop.pdf", "Coop City\nTotal CHF 12.50")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            acc = Account(name="Undo-Konto", type=AccountType.BANK, currency="CHF",
                          opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=860)
            db.add(acc)
            db.flush()
            tx = Transaction(account_id=acc.id, date=date(2024, 5, 18),
                             amount=Decimal("-12.50"), description="COOP")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        r = logged_in_client.post("/import/receipts/assign",
                                  data={"filename": "20240518_Coop.pdf", "transaction_id": tx_id})
        assert r.status_code == 200
        with SessionLocal() as db:
            att = db.scalar(select(Attachment).where(Attachment.original_name == "20240518_Coop.pdf"))
            assert att is not None
            att_id = att.id

        r2 = logged_in_client.post("/import/receipts/unassign", data={"att_id": att_id})
        assert r2.status_code == 200
        with SessionLocal() as db:
            assert db.get(Attachment, att_id) is None  # Anhang wieder weg
    finally:
        settings.receipts_dir = old


def test_receipts_page_renders_open_link(logged_in_client: TestClient, tmp_path) -> None:
    """Die Zuordnen-Seite lädt schnell (kein OCR) und rendert je Beleg den
    „Beleg öffnen"-Link (else-Zweig der Vorlage)."""
    _make_text_pdf(tmp_path / "Coop_Beleg.pdf", "Coop City\nTotal CHF 12.50")  # ohne Datum im Namen
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        r = logged_in_client.get("/import/receipts")
        assert r.status_code == 200
        assert "Beleg öffnen" in r.text
        assert "/import/receipts/file?name=" in r.text
    finally:
        settings.receipts_dir = old


def test_tx_options_search_finds_then_excludes_attached(
    logged_in_client: TestClient, tmp_path
) -> None:
    """Server-Suche fürs Zuordnen-Dropdown findet eine noch offene Buchung über Betrag
    ODER Text (beliebiges Datum) — so lässt sich eine spät per Überweisung bezahlte
    Rechnung verknüpfen statt archivieren. Nach dem Zuordnen fällt sie aus der Suche."""
    _make_text_pdf(tmp_path / "Beispielshop_Rechnung.pdf", "Beispielshop\nTotal CHF 464.20")
    old = settings.receipts_dir
    settings.receipts_dir = tmp_path
    try:
        with SessionLocal() as db:
            acc = Account(name="Such-Konto", type=AccountType.BANK, currency="CHF",
                          opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=861)
            db.add(acc)
            db.flush()
            tx = Transaction(account_id=acc.id, date=date(2024, 5, 20),
                             amount=Decimal("-464.20"), description="E-BANKING BEISPIELSHOP")
            db.add(tx)
            db.commit()
            tx_id = tx.id

        # per Betrag gefunden (Komma wie Punkt) …
        r = logged_in_client.get("/import/receipts/tx-options", params={"q": "464,20"})
        assert r.status_code == 200
        assert f'value="{tx_id}"' in r.text and "BEISPIELSHOP" in r.text
        # … per Text ebenfalls …
        r2 = logged_in_client.get("/import/receipts/tx-options", params={"q": "beispielshop"})
        assert f'value="{tx_id}"' in r2.text
        # … Unsinns-Suche → klarer Hinweis statt Option.
        r3 = logged_in_client.get("/import/receipts/tx-options", params={"q": "zzzznichts"})
        assert "keine Buchung gefunden" in r3.text

        # Nach dem Zuordnen ist die Buchung aus der Suche raus.
        logged_in_client.post("/import/receipts/assign",
                              data={"filename": "Beispielshop_Rechnung.pdf", "transaction_id": tx_id})
        r4 = logged_in_client.get("/import/receipts/tx-options", params={"q": "464,20"})
        assert f'value="{tx_id}"' not in r4.text
    finally:
        settings.receipts_dir = old
