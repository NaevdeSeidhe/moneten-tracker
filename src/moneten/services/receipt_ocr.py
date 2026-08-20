"""Text-Extraktion aus Quittungen — zweistufig.

1. **Text-Layer zuerst:** Die meisten Quittungen sind bereits durchsuchbar
   (OCR lief beim ScanSnap-Einscannen). PyMuPDF liest den vorhandenen
   Text-Layer direkt — schnell, kein erneutes OCR nötig.
2. **OCR-Fallback:** Ist ein PDF *nicht* lesbar (kein/kaum Text-Layer) oder
   handelt es sich um ein reines Bild (JPG/PNG), rendert PyMuPDF die Seite zu
   einem Bild und Tesseract liest den Text per OCR.

Tesseract ist ein natives Programm. Im Docker ist es installiert
(``tesseract-ocr`` + ``tesseract-ocr-deu``). Fehlt es (z.B. lokal auf Windows),
wird der OCR-Fallback sauber übersprungen — der Text-Layer-Pfad funktioniert
weiterhin.

Zusätzlich wird ein **Betrag** aus dem Text geschätzt (für die Auto-Zuordnung).
"""

from __future__ import annotations

import contextvars
import io
import math
import re
import shutil
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz  # PyMuPDF

from moneten.config import settings

# ---------------------------------------------------------------------------
# Bildbomben-Grenze
# ---------------------------------------------------------------------------
#: Mehr Bildpunkte nimmt die App nicht an. Pillow erlaubt von sich aus rund
#: 179 Megapixel — das sind beim Entpacken über 500 MB, und die Datei dafür ist
#: wenige Kilobyte gross (eine einfarbige Fläche komprimiert fast auf nichts).
#: Der Container hat 1 GB: ein einziges solches Bild reicht, um ihn abzuwürgen,
#: und die Grössenprüfung des Uploads (15 MB) hätte nichts gemerkt.
#:
#: 50 Megapixel sind reichlich bemessen — ein Handyfoto liegt bei 12 bis 50,
#: und für die Erkennung wird ohnehin auf 1200 Pixel Kantenlänge verkleinert.
#: **Halbiert eingetragen, weil Pillow erst beim DOPPELTEN abbricht.**
#: ``Image.MAX_IMAGE_PIXELS`` ist die Schwelle für eine *Warnung*; einen Fehler
#: wirft Pillow erst oberhalb des zweifachen Wertes. Wer 50 Millionen als Grenze
#: meint, muss 25 eintragen — sonst laufen Bilder bis 100 Megapixel durch, und
#: die Warnung liest niemand. Nachgemessen an Pillow 12.
MAX_BILDPUNKTE = 25_000_000

#: Was die Grenze bedeuten SOLL: 50 Megapixel. Steht als eigener Wert da, damit
#: die Absicht nicht in einer Division verschwindet.
GEMEINTE_OBERGRENZE = 2 * MAX_BILDPUNKTE

#: Obergrenze beim RENDERN einer PDF-Seite. ``MAX_BILDPUNKTE`` greift hier
#: nicht: das ist Pillows Schutz beim Öffnen einer Bilddatei, eine gerenderte
#: Seite entsteht dagegen im Speicher, ohne dass Pillow je gefragt wird.
#:
#: Nachgemessen: eine PDF-Seite von 3000 × 3000 Punkt — 520 Byte auf der Platte —
#: ergibt bei fest eingestellten 300 dpi eine Fläche von 12'500 × 12'500 Pixel,
#: 469 MB Rohdaten, gemessene Spitze 1091 MB. Der Container hat 1 GB. Und A0
#: (2384 × 3370 Punkt) ist kein Angriff, sondern ein gewöhnliches Planformat:
#: 419 MB. Statt abzulehnen wird die Auflösung heruntergerechnet — ein Beleg,
#: der als A0 eingescannt wurde, soll lesbar bleiben.
MAX_RENDER_PIXEL = 25_000_000

#: Wie viele Seiten eines PDFs überhaupt durch die Erkennung gehen. Der Schaden
#: ist hier Zeit, nicht Speicher: gemessen 118 ms je Seite allein fürs Rendern,
#: und die Erkennung liest je Seite bis zu vier Lagen. Ein PDF mit 10'000 Seiten
#: (rund 3,8 MB, mühelos unter der Upload-Grenze) beschäftigt den Dienst über
#: Stunden — auf der NAS-CPU deutlich länger als hier gemessen.
#:
#: 30 Seiten sind für einen Beleg oder eine Rechnung reichlich — die längste
#: Vorlage, die dieses Projekt kennt (eine Monatsrechnung mit aufgeschlüsselten
#: Positionen), hat unter zehn.
MAX_OCR_SEITEN = 30


def _pixmap(seite):  # noqa: ANN001, ANN201 — fitz.Page, nur hier gebraucht
    """Rendert eine PDF-Seite mit gedeckelter Fläche.

    300 dpi sind für Belege richtig. Bei einer übergrossen Seite wird die
    Auflösung so weit gesenkt, dass ``MAX_RENDER_PIXEL`` eingehalten wird —
    lieber ein gröber gelesener Grossformat-Plan als ein toter Container. Unter
    72 dpi geht es nicht, darunter ist ohnehin nichts mehr zu erkennen.
    """
    dpi = 300
    breite = seite.rect.width * dpi / 72
    hoehe = seite.rect.height * dpi / 72
    flaeche = breite * hoehe
    if flaeche > MAX_RENDER_PIXEL:
        dpi = max(72, int(dpi * math.sqrt(MAX_RENDER_PIXEL / flaeche)))
    return seite.get_pixmap(dpi=dpi)


def pil_image():
    """Das Pillow-Bildmodul — mit gesetzter Bildbomben-Grenze.

    Warum eine Funktion statt eines Imports oben: Pillow zieht beim Import
    einiges nach, und die App startet auch ohne Bildverarbeitung. Die Grenze
    muss aber stehen, BEVOR das erste Bild geöffnet wird — Pillow prüft die
    Ausmasse schon beim Öffnen, nicht erst beim Entpacken. Deshalb führt jeder
    Zugriff auf das Modul hier durch.
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_BILDPUNKTE
    return Image


# Ab so vielen Zeichen gilt ein Text-Layer als "lesbar" (sonst OCR-Fallback).
_MIN_TEXT_CHARS = 25

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}

# Betrag: 1'234.50 / 1234,50 / 78.40.
#
# Die drei Nachbedingungen sind je ein gemessener Fehler, kein Vorrat:
#   ``(?!\s*%)``  — auf Belegen mit MwSt-Spalte („9.50 CHF  MwSt 2.60 %") ist die
#                   rechte Zahl ein SATZ; als Betrag gelesen wäre sie der Preis.
#   ``(?!\d)``    — ein Betrag hat GENAU zwei Nachkommastellen. Ohne diese
#                   Schranke wurde aus der Bon-Nummer „172893", die OCR als
#                   „172.893" las, der Betrag 172.89 — und weil er der grösste
#                   auf dem Beleg war, das Beleg-Total. Der echte Total lautete
#                   17.25.
#   ``(?!\.\d)``  — „05.03.2026" ist ein Datum, kein Betrag 05.03.
_AMOUNT = r"\d+(?:['’ ]\d{3})*[.,]\d{2}" + r"(?!\d)(?!\.\d)(?!\s*%)"
_AMOUNT_RE = re.compile(_AMOUNT)
# Betrag in der Nähe von Total/Summe/Betrag/Gesamt (priorisiert).
_TOTAL_RE = re.compile(rf"(?:total|summe|betrag|gesamt|zu zahlen)\D{{0,20}}({_AMOUNT})", re.IGNORECASE)
# Der vom Konto belastete Betrag = „Total EFT CHF" / „Total-EFT CHF" (EFT = der per
# Karte abgebuchte Betrag). DAS zeigt die Bank — bei TEILZAHLUNG (z.B. Geschenkkarte +
# Restbetrag Karte) weicht es vom Beleg-Total ab. Für die Zuordnung daher VORRANG.
_EFT_TOTAL_RE = re.compile(rf"total[\s\-]*eft[\s\-]*chf\s*[:.;=]*\s*({_AMOUNT})", re.IGNORECASE)
# Sonst die Grand-Total-Zeile „Total/Gesamt/Endbetrag CHF X". Bewusst mit „CHF", damit
# „Sie sparen total 15.15" (Ersparnis) und „Zwischentotal" NICHT als Betrag gelesen
# werden (``\b`` schliesst „Zwischentotal" aus, fehlendes „CHF" die Ersparnis-Zeile).
_GRAND_TOTAL_RE = re.compile(
    rf"\b(?:total|gesamt|endbetrag|summe)\s*chf\b\s*[:.;=*]*\s*({_AMOUNT})", re.IGNORECASE)
# Kartenzahlungs-Zeile „Visa CHF 200.85" / „Mastercard … 49.90" = die abgebuchte Summe.
# Hilft, wenn die Beleg-Summe per OCR verfälscht ist, der Kartenbetrag aber lesbar bleibt.
# Fenster grosszügig (30), weil zwischen „Visa" und Betrag oft „CHF" + viele Spalten-
# Leerzeichen stehen (z. B. „Visa            CHF        24.90").
_CARD_TOTAL_RE = re.compile(
    rf"\b(?:[vu]isa|mastercard|master|maestro|kreditkarte|postcard)\b[^\d\n]{{0,30}}({_AMOUNT})", re.IGNORECASE)
# Schweizer UID/MWST-Nummer „CHE-000.000.000" / „CHE-116 311.105". Ihre Ziffern sehen wie
# ein Betrag aus (→ „151.85") und würden sonst vom „grösste Zahl"-Fallback als Total
# gegriffen. Wird darum vor dem Fallback aus dem Text entfernt.
_UID_RE = re.compile(r"CHE[-\s]?\d{3}[.\s]?\d{3}[.\s]?\d{3}", re.IGNORECASE)
# „Kartenzahlung CHF 12.75" (auch OCR-getrennt „Kartenzahl ung CHF") = der per Karte
# abgebuchte Betrag → genau das, was die Bank in der Buchung sieht. Whitespace wird vorher
# normalisiert (Spalten/Zeilenumbruch), ``(?![.,]\d)`` verwirft Datums-Fragmente.
_PAYMENT_TOTAL_RE = re.compile(rf"kartenzahl\w*[^\d]{{0,20}}({_AMOUNT})(?![.,]\d)", re.IGNORECASE)
# Datums-/Zeit-Fragmente (31.12.26 / 10:15) VOR dem „grösste Zahl"-Fallback entfernen —
# sonst läse „31.12.26 10:15" den Betrag 31.12.
_DATE_STRIP = re.compile(r"\d{1,2}[.,]\d{1,2}[.,]\d{2,4}|\b\d{1,2}:\d{2}\b")
# Rechnungs-Total auf RECHNUNGEN (nicht Kassenbons): „Gesamtbetrag (Brutto): CHF 114.70",
# „… (Zahlungsbetrag) 64.30", „Rechnungsbetrag/Endbetrag …". Bewusst SPEZIFISCHE Substantive
# (nicht bloss „Betrag"/„Total"/„Saldo"), damit Spalten-Header, Zwischensummen und Karten-/
# Kontosaldi nicht fälschlich greifen. Da Label und Betrag oft durch Spalten/Zeilenumbruch
# getrennt sind, wird der Text vorher per ``_WS_RE`` whitespace-normalisiert. ``[^\d]{0,30}``
# überbrückt „(Brutto): CHF " o.ä., ``(?![.,]\d)`` verwirft Datums-Fragmente (z. B. „11.09"
# aus „11.09.2024" hinter „zu zahlen bis").
_INVOICE_TOTAL_RE = re.compile(
    rf"(?:gesamtbetrag|gesamttotal|rechnungstotal|rechnungsbetrag|zahlungsbetrag|endbetrag|"
    rf"zu\s+bezahlen|zu\s+zahlen)[^\d]{{0,30}}({_AMOUNT})(?![.,]\d)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# Datum aus dem Belegtext (Fallback, wenn der Dateiname kein Datum trägt).
# Komma als Trenner zugelassen (OCR liest „28.11.2024" oft als „28,11.2024"). Ein
# 2-teiliger Komma-Betrag wie „611,00" matcht NICHT (Datum braucht zwei Trenner).
# ``\b``-Grenzen, damit das Datum nicht aus einer längeren Referenz-/Kartennummer
# „herausgeschnitten" wird (z. B. „…B300/31.12.2026/…": ohne Grenzen matchte fälschlich
# „00/31.12" und das echte „31.12.2026" ging verloren).
_DATE_ISO = re.compile(r"\b(\d{4})[.,\-/](\d{1,2})[.,\-/](\d{1,2})\b")        # 2026-05-15
_DATE_NUM = re.compile(r"\b(\d{1,2})[.,\-/](\d{1,2})[.,\-/](\d{2,4})\b")      # 15.05.2026 / 28,11.2024
_DATE_DE = re.compile(r"(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜ]{3,})\.?\s+(\d{4})")  # 15. Mai 2026
_DATE_KEYWORD = re.compile(r"datum|date|kauf|beleg", re.IGNORECASE)
# Datum MIT Uhrzeit daneben = Transaktions-Zeitstempel → das eigentliche Belegdatum
# (robust gegen „Punktestand per 30.09." u.ä. ohne Uhrzeit). Toleriert Komma-Trenner
# und ein „Zeit:"-Label zwischen Datum und Uhrzeit.
_DATE_NUM_TIME = re.compile(
    r"\b(\d{1,2})[.,\-/](\d{1,2})[.,\-/](\d{2,4})\b[\s,/]{0,3}(?:zeit\.?:?\s*)?\d{1,2}:\d{2}", re.IGNORECASE)
# Dasselbe, aber OHNE Trennung: OCR klebt Datum, Uhrzeit und Bon-Nummer zu einer
# Kette zusammen. Gemessen an einer Fusszeile der Form „05.03.202609:1500112233
# Kasse 1/4" — das Datum war da, kein Muster fand es, und der Beleg kam ohne
# Datum in den Editor. Das Jahr
# muss hier vierstellig sein, weil ohne Trennzeichen sonst nicht zu sagen ist,
# wo es aufhoert.
_DATE_NUM_GEKLEBT = re.compile(r"(\d{1,2})[.,\-/](\d{1,2})[.,\-/](\d{4})(?=\d{1,2}:\d{2})")
_DATE_ISO_TIME = re.compile(
    r"\b(\d{4})[.,\-/](\d{1,2})[.,\-/](\d{1,2})\b[\s,/]{0,3}(?:zeit\.?:?\s*)?\d{1,2}:\d{2}", re.IGNORECASE)
_MONTHS_DE = {  # Prefix (erste 3 Zeichen, lowercase) → Monat
    "jan": 1, "feb": 2, "mär": 3, "mae": 3, "mrz": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}


@dataclass
class OcrResult:
    """Ergebnis der Text-Extraktion."""

    text: str
    method: str  # "text-layer" | "ocr" | "none"
    amount: Decimal | None  # geschätzter Gesamtbetrag
    date: date | None = None  # aus dem Text geschätztes Belegdatum


def tesseract_available() -> bool:
    """True, wenn das Tesseract-Programm im PATH gefunden wird."""
    return shutil.which("tesseract") is not None


def ocr_available() -> bool:
    """True, wenn IRGENDEINE OCR-Engine läuft — RapidOCR (primär, gebündelte Modelle)
    oder Tesseract. Die OCR-Pfade dürfen nicht an ``tesseract_available()`` hängen,
    sonst bleibt OCR fälschlich aus, wenn nur RapidOCR installiert ist."""
    return _rapidocr_engine() is not None or tesseract_available()


def _to_decimal(raw: str) -> Decimal | None:
    s = raw.replace("'", "").replace("’", "").replace(" ", "")
    s = s.replace(",", ".") if ("," in s and "." not in s) else s.replace(",", "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def extract_amount(text: str) -> Decimal | None:
    """Schätzt den Gesamtbetrag aus dem Quittungstext.

    Priorität (jeweils der Betrag, den die BANK belastet — darauf matcht die Zuordnung):
      1. **„Total EFT CHF X"** — der per Karte abgebuchte Betrag. Bei Teilzahlung
         (Geschenkkarte + Karte) zeigt die Bank nur diesen Teil, nicht das Beleg-Total.
      2. **Rechnungs-Total** „Gesamtbetrag/Zahlungsbetrag/Rechnungsbetrag/Endbetrag X" —
         auch wenn Label und Betrag durch Spalten/Zeilen getrennt sind. Vorrang vor dem
         Fallback, damit bei mehrseitigen Rechnungen/Einzahlungsscheinen nicht eine
         grössere Referenz-/Positions-/Seite-2-Zahl gegriffen wird.
      3. **„Total/Gesamt/Endbetrag CHF X"** — bei voller Kartenzahlung = Bankbetrag.
         Bewusst mit „CHF", damit „Sie sparen total" (Ersparnis) und „Zwischentotal"
         nicht greifen (der 15.15-statt-126.50-Bug).
      4. Fallback: grösster gefundener Betrag (die Endsumme ist meist der grösste —
         fängt z. B. OBI „Endsumme in SFr" und Belege ohne „Total CHF" ab).
    """
    if not text:
        return None
    eft = [v for v in (_to_decimal(m) for m in _EFT_TOTAL_RE.findall(text)) if v is not None]
    if eft:
        return eft[-1]
    collapsed = _WS_RE.sub(" ", text)  # Spalten/Zeilen zusammenfassen (Label + Betrag finden sich)
    # „Kartenzahlung CHF X" = per Karte abgebuchter Betrag (= Banktext-Betrag) → Vorrang.
    payment = [v for v in (_to_decimal(m) for m in _PAYMENT_TOTAL_RE.findall(collapsed)) if v is not None]
    if payment:
        return payment[-1]
    # Rechnungs-Total. Letzter Treffer = der finale Zahlbetrag am Belegende.
    invoice = [v for v in (_to_decimal(m) for m in _INVOICE_TOTAL_RE.findall(collapsed)) if v is not None]
    if invoice:
        return invoice[-1]
    grand = [v for v in (_to_decimal(m) for m in _GRAND_TOTAL_RE.findall(text)) if v is not None]
    if grand:
        return max(grand)
    card = [v for v in (_to_decimal(m) for m in _CARD_TOTAL_RE.findall(text)) if v is not None]
    if card:
        return max(card)
    # Fallback: grösster plausibler Betrag. Vorher raus: UID/MWST-Nummern (sonst läse
    # „CHE-000.000.000" als 151.85) UND Datums-/Zeit-Fragmente (sonst „31.12.26" → 31.12);
    # die Obergrenze hält grosse Konto-/Referenznummern draussen.
    cleaned = _DATE_STRIP.sub(" ", _UID_RE.sub(" ", text))
    candidates = [d for d in (_to_decimal(x) for x in _AMOUNT_RE.findall(cleaned))
                  if d is not None and d < Decimal("100000")]
    return max(candidates) if candidates else None


def _safe_date(y: int, m: int, d: int) -> date | None:
    """Baut ein Datum, prüft Plausibilität (Jahr 2000–2099). None bei ungültig."""
    if y < 100:  # 2-stelliges Jahr → 20yy (Belege sind aktuell)
        y += 2000
    if not (2000 <= y <= 2099):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_date(text: str) -> date | None:
    """Schätzt das Belegdatum aus dem Text (Fallback zum Dateinamen).

    Erkennt ``2026-05-15``, ``15.05.2026``, ``15.05.26`` und ``15. Mai 2026``.

    Priorität: (1) ein Datum **mit Uhrzeit daneben** (Transaktions-Zeitstempel = das
    eigentliche Belegdatum), (2) ein Datum direkt nach einem Schlüsselwort
    (Datum/Kauf/Beleg), (3) das erste plausible im Text.
    """
    if not text:
        return None
    # (1) Datum mit Uhrzeit daneben — das verlässlichste Signal für das Belegdatum.
    for rgx, order in ((_DATE_NUM_TIME, ("d", "m", "y")),
                       (_DATE_NUM_GEKLEBT, ("d", "m", "y")),
                       (_DATE_ISO_TIME, ("y", "m", "d"))):
        for m in rgx.finditer(text):
            parts = dict(zip(order, m.groups(), strict=True))
            d = _safe_date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
            if d is not None:
                return d
    cands: list[tuple[int, date]] = []  # (Position, Datum)
    for m in _DATE_ISO.finditer(text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            cands.append((m.start(), d))
    for m in _DATE_NUM.finditer(text):
        d = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            cands.append((m.start(), d))
    for m in _DATE_DE.finditer(text):
        mon = _MONTHS_DE.get(m.group(2)[:3].lower())
        if mon:
            d = _safe_date(int(m.group(3)), mon, int(m.group(1)))
            if d:
                cands.append((m.start(), d))
    if not cands:
        return None
    # Bevorzugt das Datum, das einem Schlüsselwort am nächsten folgt.
    keywords = [k.end() for k in _DATE_KEYWORD.finditer(text)]
    if keywords:
        best, best_dist = None, 10**9
        for pos, d in cands:
            for k in keywords:
                if pos >= k and (pos - k) < best_dist:
                    best, best_dist = d, pos - k
        if best is not None:
            return best
    cands.sort(key=lambda c: c[0])
    return cands[0][1]


def _ocr_lang() -> str:
    """Tesseract-Sprachen (konfigurierbar, Default deutsch+englisch)."""
    return settings.ocr_lang if getattr(settings, "ocr_lang", None) else "deu+eng"


def _pdf_text_layer(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _ocr_pdf(path: Path) -> str:
    """Rendert jede PDF-Seite (300 dpi) und liest sie per OCR — über DIESELBE robuste
    Pipeline wie der Foto-Upload (Vorverarbeitung + Aufbereitungs-Fallback), nicht roh.
    Gescannte Belege ohne Text-Layer werden so zuverlässig gelesen."""
    Image = pil_image()

    doc = fitz.open(path)
    parts: list[str] = []
    try:
        for page in list(doc)[:MAX_OCR_SEITEN]:
            pix = _pixmap(page)
            parts.append(_ocr_pil_image(Image.open(io.BytesIO(pix.tobytes("png")))))
    finally:
        doc.close()
    return "\n".join(parts)


def _preprocess(image, *, binarize: bool = True):
    """Bereitet ein (Handy-)Foto fürs OCR auf — Best-Practice-Pipeline, rein mit PIL
    (kein OpenCV/numpy, NAS-freundlich):

      1. EXIF-Drehung korrigieren, Graustufen.
      2. Hochskalieren (≈300-dpi-Äquivalent) → mehr Pixel pro Zeichen.
      3. Entrauschen (Median) + Kontrast spreizen + leicht schärfen.
      4. **Adaptive Binarisierung** (lokaler Hintergrund via BoxBlur): macht den Text
         schwarz/weiss relativ zu seiner Umgebung → robust gegen ungleiche Ausleuchtung,
         Schatten und Wölbung von Thermo-Belegen (der grösste Genauigkeits-Hebel laut
         Praxis). Mit Plausibilitäts-Check: kippt die Binarisierung (fast alles
         schwarz/weiss), wird die Graustufen-Variante genutzt.

    Jeder Schritt ist tolerant — schlägt etwas fehl, wird das beste Zwischenergebnis
    weitergereicht (nie ein Crash)."""
    from PIL import ImageChops, ImageFilter, ImageOps

    try:
        img = ImageOps.exif_transpose(image)
    except Exception:
        img = image
    img = img.convert("L")
    w, h = img.size
    longest = max(w, h) or 1
    # Längste Kante auf 2400 px normalisieren: genug Auflösung für Beleg-Text, aber
    # klein genug, dass Tesseract auch im Container nicht zu viel Speicher braucht
    # (riesige 12-MP-Fotos sonst → OOM → „kein Text"). Lokal an echten Belegen getestet:
    # 2400 liest die Beträge korrekt, 2800 verschätzt sich teils, 1600 ist zu klein.
    target = 2400
    if longest != target:
        scale = target / longest
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        longest = max(img.size)
    try:
        img = img.filter(ImageFilter.MedianFilter(3))                          # Speckle/Rauschen
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
        if binarize:
            radius = max(12, round(longest / 60))                              # Fenster ≈ Zeilenhöhe
            bg = img.filter(ImageFilter.BoxBlur(radius))                       # lokaler Hintergrund
            bw = ImageChops.subtract(bg, img).point(lambda p: 0 if p > 12 else 255)  # adaptiver Schwellwert
            px = (bw.size[0] * bw.size[1]) or 1
            black_ratio = bw.histogram()[0] / px
            if 0.01 <= black_ratio <= 0.6:  # plausibel → binarisiert nutzen, sonst Graustufe
                img = bw
    except Exception:
        pass
    return img


def _ocr_score(t: str) -> int:
    """Heuristik für die OCR-Qualität: mehr erkannte Beträge + mehr Text = besser.
    Dient dazu, aus mehreren Varianten (PSM-Modi, mit/ohne Binarisierung) die beste
    zu wählen."""
    return len(_AMOUNT_RE.findall(t)) * 12 + len(t)


def _orientation_score(text: str) -> int:
    """Lesbarkeits-Score für die Orientierungswahl: zählt **Geldbeträge und echte Wörter**
    (NICHT die reine Textlänge — um 90°/180° gedrehter Text ergibt oft VIEL Zeichen-Müll,
    aber kaum gültige Beträge/Wörter; ein längen-basierter Score würde ihn fälschlich
    bevorzugen)."""
    amounts = len(_AMOUNT_RE.findall(text))
    words = len(re.findall(r"[A-Za-zÄÖÜäöü]{3,}", text))
    keywords = len(re.findall(r"chf|total|eft|mwst|summe", text, re.I))
    return amounts * 10 + words + keywords * 5


def _reading_direction_score(text: str) -> int:
    """Zählt Zeilen mit Muster „Buchstaben … Betrag" (Name LINKS, Preis RECHTS). Hoch bei
    KORREKTER Lage, niedrig bei 180° (dort steht der Preis links). Unterscheidet 0° von 180°
    — was reine Betrags-/Wortanzahl NICHT kann, weil RapidOCR dank Zeilen-Winkel-Korrektur
    auch kopfüber sauberen Text liest (nur die Spalten-Reihenfolge bleibt gespiegelt)."""
    n = 0
    for line in text.splitlines():
        m = _AMOUNT_RE.search(line)
        if m and re.search(r"[A-Za-zÄÖÜäöü]{2,}", line[: m.start()]):
            n += 1
    return n


# ---- OCR-Engine: RapidOCR (ONNX) bevorzugt, Tesseract als Fallback -----------------
# RapidOCR fährt die PaddleOCR-Modelle über ONNX-Runtime: GEBÜNDELTE Modelle = überall
# identisch (NAS wie lokal, kein „bei mir geht's"), deutlich robuster auf rauschigen
# Thermo-Bons als Tesseract. Tesseract bleibt als Fallback, falls das Paket fehlt.
_RAPIDOCR_ENGINE = None
_RAPIDOCR_INIT = False


def _rapidocr_engine():
    """Lazy-Singleton der RapidOCR-Engine (Modell-Load ist teuer → einmal). None, wenn das
    Paket nicht installiert ist oder nicht lädt — dann Fallback auf Tesseract."""
    global _RAPIDOCR_ENGINE, _RAPIDOCR_INIT
    if not _RAPIDOCR_INIT:
        _RAPIDOCR_INIT = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPIDOCR_ENGINE = RapidOCR()
        except Exception:
            _RAPIDOCR_ENGINE = None
    return _RAPIDOCR_ENGINE


def _box_geometrie(box) -> tuple[float, float, float, float, float | None]:
    """(y-Mitte, x-Mitte, x-links, Zeilenhöhe, Steigung) einer OCR-Box.

    RapidOCR liefert ein VIERECK — vier Ecken im Uhrzeigersinn ab links oben —, das
    bei schräg gehaltenem Handy mitgeneigt ist. Daraus folgen zwei Dinge, die die
    Zeilenbildung braucht:

    * Die **Zeilenhöhe** wird an der Seitenkante gemessen, nicht als achsenparallele
      Ausdehnung. Letztere wächst mit der Breite der Box: nachgemessen meldet eine
      286 px breite Namens-Box bei 3.5° Neigung 40 px Höhe statt 23 — und über den
      Median blähte das die Zeilen-Toleranz auf, bis die nächste Bon-Zeile hineinfiel.
    * Die **Steigung der Oberkante** ist die Neigung der Textzeile selbst (nachgemessen
      0.060 bei 3.5°, erwartet tan 3.5° = 0.061). Damit lässt sich der Beleg rechnerisch
      geradeziehen, statt die Neigung mit einer grösseren Toleranz zu erschlagen.

    Bei einer Box, die kein Viereck ist, fällt beides auf die achsenparallele
    Ausdehnung zurück und die Steigung entfällt (``None``).
    """
    punkte = [(float(p[0]), float(p[1])) for p in box]
    xs = [p[0] for p in punkte]
    ys = [p[1] for p in punkte]
    mitte = (sum(ys) / len(ys), sum(xs) / len(xs), min(xs))
    if len(punkte) != 4:
        return (*mitte, max(ys) - min(ys), None)
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = punkte
    hoehe = (math.dist((x0, y0), (x3, y3)) + math.dist((x1, y1), (x2, y2))) / 2
    breite = x1 - x0
    # Schmale Boxen („1", „2.90") messen die Neigung zu grob (1 px Rundung auf 50 px
    # Breite sind schon 0.02) — die Auswahl der brauchbaren trifft der Aufrufer.
    return (*mitte, hoehe, (y1 - y0) / breite if breite > 1 else None)


# Kleinste Zeilenhoehe, mit der noch gerechnet wird. Darunter misst nichts mehr:
# ein Fetzen dieser Hoehe ist ein Artefakt, kein Text.
_MIN_ZEILENHOEHE = 6.0

# Ob die Neigung des zuletzt gelesenen Belegs wirklich gemessen wurde.
#
# ``ContextVar`` und NICHT eine gewoehnliche Modulvariable: die App bedient
# mehrere Anfragen nebenlaeufig (Starlette schickt synchrone Endpunkte in einen
# Threadpool). Eine geteilte Variable haette der zweite Beleg dem ersten unter
# den Fuessen weggezogen — die Auskunft haette dann zu irgendeinem Beleg gehoert.
# Jede Anfrage bekommt ihre eigene Kopie des Kontexts, also ihre eigene Antwort.
#
# Ein Rueckgabewert waere sauberer, hiesse aber, den Wert durch vier Ebenen zu
# faedeln, die alle nur Text durchreichen.
_NEIGUNG_GEMESSEN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "neigung_gemessen", default=True
)


def neigung_gemessen() -> bool:
    """Wurde die Schraeglage des zuletzt gelesenen Belegs gemessen?

    ``False`` heisst: zu wenige breite Textboxen. Der Beleg wurde dann UNGEDREHT
    zerlegt — bei einem schief fotografierten Bon ist das genau der Fall, in dem
    Zeilen verrutschen. Die Oberflaeche kann damit sagen, WARUM sie nichts
    Verlaessliches anzeigt, statt nur „ungeprueft".
    """
    return _NEIGUNG_GEMESSEN.get()


def _layout_text(result, *, min_conf: float = 0.5) -> str:
    """Baut aus RapidOCRs (Box, Text, Confidence)-Fetzen einen ZEILEN-strukturierten Text:
    Fetzen derselben Bon-Zeile werden zusammengefasst (innerhalb der Zeile links→rechts
    sortiert), so dass **Name und Preis wieder auf derselben Zeile** stehen → zuverlässiges
    Positions-Parsing. Sehr unsichere Fetzen (Confidence < min_conf) werden verworfen.
    Reine Geometrie, kein NN.

    Zwei Dinge entscheiden, ob das gelingt:

    1. Der Beleg wird zuerst **geradegezogen** (Neigung aus den Boxkanten, siehe
       :func:`_box_geometrie`). Ohne das läuft die y-Mitte einer Zeile von links nach
       rechts weg — bei 3.5° über die Belegbreite um fast einen ganzen Zeilenschritt.
    2. Der Zeilenanker steht **fest** auf dem ersten Fetzen der Zeile. Vorher war er der
       laufende Mittelwert und wanderte mit jedem Fetzen nach unten; sobald ein Fetzen
       der nächsten Zeile dadurch in die Toleranz rutschte, verschmolzen zwei Bon-Zeilen
       (zwei Produktnamen in einer Position) und alles danach war um eine Zeile versetzt.
    """
    frags: list[tuple[float, float, float, str]] = []  # (y-Mitte, x-Mitte, x-links, Text)
    hoehen: list[float] = []
    neigungen: list[tuple[float, float]] = []          # (Breite, Steigung)
    for box, text, conf in result:
        if not text or not text.strip() or conf < min_conf:
            continue
        y, x, links, hoehe, steigung = _box_geometrie(box)
        frags.append((y, x, links, text.strip()))
        hoehen.append(hoehe)
        if steigung is not None:
            neigungen.append(((x - links) * 2, steigung))  # x-Mitte minus x-links = halbe Breite
    if not frags:
        return ""
    hoehen.sort()
    # Unteres Quartil statt Median: es beschreibt den Fliesstext auch dann noch, wenn
    # ein guter Teil der Fetzen aus einem gross gesetzten Kopf stammt.
    # ``or 30.0`` fing nur die glatte Null ab. Eine Box von 2 px Hoehe ist aber
    # ebenso unbrauchbar als Mass: die Schranke fuer „breit genug zum Messen"
    # laege dann bei 6 px, und JEDER Fetzen zaehlte als breit — die Neigung
    # kaeme aus lauter Kurzfetzen, also aus Rauschen.
    basis = max(hoehen[len(hoehen) // 4], _MIN_ZEILENHOEHE)
    # Nur Boxen ab dreifacher Zeilenhöhe messen die Neigung brauchbar; der Median über
    # mehrere davon fängt einzelne Ausreisser (schief erkannte Kurzfetzen) ab.
    breit = [s for b, s in neigungen if b >= basis * 3]
    # Weniger als drei breite Boxen: die Neigung ist NICHT gemessen, sondern
    # aufgegeben. Vorher stand hier stillschweigend 0.0 — bei 3-5 Grad Schraeglage
    # kam damit der Zeilenversatz zurueck, und der Nutzer sah nur „ungeprueft"
    # ohne einen Grund. Der Aufrufer erfaehrt es jetzt (siehe ``neigung_gemessen``).
    gemessen = len(breit) >= 3
    neigung = statistics.median(breit) if gemessen else 0.0
    _NEIGUNG_GEMESSEN.set(gemessen)
    gerade = sorted((y - neigung * x, links, t) for y, x, links, t in frags)
    # Der Zeilenabstand wäre das direktere Mass — nur ist er ohne fertige Zeilen nicht
    # messbar (Henne/Ei), und nach dem Geradeziehen liegt die richtig gemessene
    # Zeilenhöhe nachweislich deutlich unter dem Abstand (nachgemessen 0.75–1.05 davon).
    tol = basis * 0.6
    zeilen: list[list[tuple[float, float, str]]] = [[gerade[0]]]
    anker = gerade[0][0]  # FESTER Anker: die erste Zeilenhöhe der Zeile, kein Mittelwert
    for f in gerade[1:]:
        if f[0] - anker <= tol:
            zeilen[-1].append(f)
        else:
            zeilen.append([f])
            anker = f[0]
    return "\n".join(" ".join(t for _, _, t in sorted(z, key=lambda f: f[1])) for z in zeilen)


def _rapidocr_text(image) -> str | None:
    """Liest ein (richtig gedrehtes) PIL-Bild mit RapidOCR → zeilen-strukturierter Text
    (siehe :func:`_layout_text`). None, wenn die Engine fehlt/scheitert (Tesseract-Fallback)."""
    engine = _rapidocr_engine()
    if engine is None:
        return None
    try:
        import numpy as np
        arr = np.array(image.convert("RGB"))[:, :, ::-1]  # RGB → BGR (OpenCV-Konvention)
        result, _ = engine(arr)
        return _layout_text(result) if result else ""
    except Exception:
        return None


def _ocr_engine(image) -> str:
    """OCR EINER (bereits richtig gedrehten) Lage — RapidOCR bevorzugt, Tesseract-Fallback."""
    text = _rapidocr_text(image)
    return text if text is not None else _ocr_oriented(image)


def _ocr_oriented(image) -> str:
    """OCR eines bereits richtig gedrehten Bilds: voll aufbereitet (inkl. adaptiver
    Binarisierung); ist das Ergebnis leer/schwach, Rückfall auf die Variante OHNE
    Binarisierung — das bessere Resultat gewinnt."""
    best = _run_tesseract(_preprocess(image))
    if not best.strip() or len(_AMOUNT_RE.findall(best)) < 3:
        plain = _run_tesseract(_preprocess(image, binarize=False))
        if _ocr_score(plain) > _ocr_score(best):
            best = plain
    return best


# So viele Geldbeträge muss eine Lesung hergeben, damit die übrigen Lagen
# entfallen dürfen. Ein Kassenbon hat mehrere; drei trennen ihn von einer Lage,
# in der die Engine nur zufällig ein paar Ziffern erwischt hat.
_GENUG_BETRAEGE = 3


def _ocr_pil_image(image) -> str:
    """OCR eines (Handy-)Fotos — **dreh-unabhängig**.

    Von oben fotografierte Belege liegen mal hoch, mal quer (±90°), selten
    kopfüber, und die EXIF-Drehung ist dann unzuverlässig. Wir lesen daher alle
    vier Orientierungen mit der Engine (RapidOCR, sonst Tesseract) und nehmen
    die **ergiebigste** Lesung (Score = Geldbeträge + echte Wörter). RapidOCR
    erkennt Text auch schräg — die reine Betrags-Anzahl reicht also nicht zum
    frühen Abbruch, deshalb bewusst alle Lagen vergleichen.

    **Zwei Abkürzungen wurden ausprobiert und verworfen** (gemessen
    an einem 12-MP-Testbon mit zwanzig Zeilen, Referenz 19/19 Beträgen):

    * *Die Lage an einer kleinen Probe entscheiden.* Bringt fast nichts: 1.1×.
      Die Erkennung kostet pro TEXTKASTEN, nicht pro Pixel — ein kleineres Bild
      mit denselben zwanzig Zeilen ist kaum billiger.
    * *Die Lage ohne OCR über ein Projektionsprofil bestimmen und nur zwei Lagen
      lesen.* 2× schneller, aber nur noch 8 von 19 Beträgen. Die Lesung EINER
      Lage ist also nicht bloss langsamer als die beste von vieren — sie ist
      schlechter. Welche Lage am meisten hergibt, lässt sich vorher nicht
      ansehen.

    **Was stattdessen hilft: früh aufhören.** Dieselbe Messung zeigte, dass die
    Leserichtung sauber trennt — bei der richtigen Lage 19, bei allen anderen 0.
    Sobald eine Lage richtig herum UND ergiebig ist, sind die übrigen darum
    nicht mehr nötig. Ein aufrecht fotografierter Beleg (der Normalfall) kostet
    damit EINE Lesung statt vier, ein querliegender zwei. Findet keine Lage ein
    klares Ergebnis, werden weiterhin alle vier gelesen — die Abkürzung nimmt
    nichts weg, sie hört nur auf, wenn nichts mehr zu gewinnen ist.
    """
    from PIL import ImageOps
    try:
        upright = ImageOps.exif_transpose(image)  # EXIF-Drehung EINMAL anwenden (falls vorhanden)
    except Exception:
        upright = image

    best_text, best_key = "", (-1, -1)
    for deg in (0, 270, 90, 180):  # aufrecht, dann quer (±90°), dann kopfüber
        cand = _ocr_engine(upright if deg == 0 else upright.rotate(deg, expand=True))
        # Schlüssel: ZUERST die Leserichtung (Name links/Preis rechts → trennt 0°
        # von 180°, das RapidOCR sonst gleich gut liest), DANN die Lesbarkeit.
        key = (_reading_direction_score(cand), _orientation_score(cand))
        if key > best_key:
            best_text, best_key = cand, key
        if key[0] > 0 and len(_AMOUNT_RE.findall(cand)) >= _GENUG_BETRAEGE:
            break  # richtig herum UND ergiebig — die restlichen Lagen sparen
    return best_text


def _ocr_image(path: Path) -> str:
    Image = pil_image()

    return _ocr_pil_image(Image.open(path))


def _run_tesseract(image) -> str:
    """Tesseract auf EINEM (bereits aufbereiteten) Bild — mit progressivem Fallback,
    damit OCR nie komplett ausfällt:

      PSM 6 („einheitlicher Block") → bei wenig Beträgen zusätzlich PSM 4 („einspaltig,
      variable Grössen", beleg-typisch), das ergiebigere gewinnt → liefert *gar nichts*
      ein Treffer, ein letzter Default-Aufruf ganz ohne Optionen.

    Bewusst KEIN ``--oem``: der Default nutzt in Tesseract 4/5 ohnehin LSTM, und ein
    explizites ``--oem 1`` wirft auf manchen Builds einen Fehler, der den ganzen Lauf
    leer zurückgibt. ``preserve_interword_spaces`` hält die Spalten (Name | Preis).
    Sprach-Fallback auf die Default-Sprache, wenn das deu-Modell fehlt."""
    import pytesseract

    def attempt(cfg: str) -> str:
        try:
            return pytesseract.image_to_string(image, lang=_ocr_lang(), config=cfg)
        except pytesseract.TesseractError:
            try:
                return pytesseract.image_to_string(image, config=cfg)  # ohne deu-Modell
            except Exception:
                return ""
        except Exception:
            return ""

    best = attempt("--psm 6 -c preserve_interword_spaces=1")
    if len(_AMOUNT_RE.findall(best)) < 3:  # wenig Beträge → zweite Segmentierung probieren
        alt = attempt("--psm 4 -c preserve_interword_spaces=1")
        if _ocr_score(alt) > _ocr_score(best):
            best = alt
    if not best.strip():  # nichts erkannt → simpelster Default-Aufruf als letzte Rettung
        best = attempt("")
    return best


def _ocr_diagnostics(image) -> str:
    """Erzeugt einen Diagnose-Block, der in den „OCR-Rohtext" gelegt wird, WENN kein
    Text erkannt wurde. Zeigt, woran es scheitert: Tesseract vorhanden/Version,
    Bildgrösse und je Aufbereitungs-/Config-Variante die Zeichenzahl bzw. der Fehler.
    Läuft immer durch (jede Zeile gekapselt) — nie ein Crash."""
    out = ["[OCR-Diagnose] Kein Text erkannt — diesen Block bitte an die Entwicklung schicken."]
    try:
        out.append(f"tesseract im PATH: {shutil.which('tesseract')}")
    except Exception as e:  # noqa: BLE001
        out.append(f"tesseract im PATH: FEHLER {e!r}")
    try:
        import pytesseract

        out.append(f"Tesseract-Version: {pytesseract.get_tesseract_version()}")
    except Exception as e:  # noqa: BLE001
        out.append(f"Tesseract-Version: FEHLER {e!r}")
        return "\n".join(out)  # ohne Tesseract bringen die Bild-Tests nichts
    try:
        from PIL import ImageOps

        g = ImageOps.exif_transpose(image).convert("L")
        out.append(f"Bildgrösse (Original): {g.size[0]}x{g.size[1]}")
    except Exception as e:  # noqa: BLE001
        out.append(f"Bildgrösse: FEHLER {e!r}")
    for label, binar in (("binarisiert", True), ("ohne Binarisierung", False)):
        try:
            prepped = _preprocess(image, binarize=binar)
        except Exception as e:  # noqa: BLE001
            out.append(f"{label}: Vorverarbeitung FEHLER {e!r}")
            continue
        for cfg in ("--psm 6 -c preserve_interword_spaces=1", ""):
            try:
                t = pytesseract.image_to_string(prepped, lang=_ocr_lang(), config=cfg).strip()
                out.append(f"{label} | cfg={cfg or 'default'} | {len(t)} Zeichen | {t[:60]!r}")
            except Exception as e:  # noqa: BLE001
                out.append(f"{label} | cfg={cfg or 'default'} | FEHLER {e!r}")
    return "\n".join(out)


# Kleiner In-Memory-OCR-Cache (Pfad+mtime → Ergebnis). Verhindert, dass dieselbe
# Datei bei jedem Seitenaufruf neu ge-OCR-t wird (teuer auf der NAS). Single-User /
# eine Instanz → wie die Job-Verwaltung bewusst nur im Speicher, kein Persistenz-Bedarf.
_OCR_CACHE: dict[str, OcrResult] = {}


def _ocr_cache_key(path: Path) -> str:
    try:
        return f"{path}|{int(path.stat().st_mtime)}"
    except OSError:
        return str(path)


def extract_text(file_path: str, *, ocr: bool = True) -> OcrResult:
    """Wie :func:`_extract_text_uncached`, aber mit In-Memory-Cache (einmal pro Datei
    rechnen). Ein leeres ``ocr=False``-Ergebnis wird NICHT gecacht, damit ein späterer
    Voll-Lauf (Auto-Abgleich) die Datei doch noch per OCR auswerten kann."""
    path = Path(file_path)
    if not path.is_file():
        return OcrResult("", "none", None)
    key = _ocr_cache_key(path)
    cached = _OCR_CACHE.get(key)
    if cached is not None:
        return cached
    result = _extract_text_uncached(file_path, ocr=ocr)
    # NUR vollwertige Ergebnisse cachen: (a) kein „none" UND (b) bei ocr=False nur einen
    # ECHTEN Text-Layer (>= _MIN_TEXT_CHARS). Sonst vergiftet ein Schnell-Lauf den Cache —
    # z. B. PDF mit Mini-Text-Layer (nur Seitenzahl): ocr=False cached das Fragment, und
    # der spätere Voll-Lauf (Auto-Abgleich) würde die Datei nie mehr per OCR lesen.
    full = ocr or (result.method == "text-layer"
                   and (len(result.text.strip()) >= _MIN_TEXT_CHARS or result.amount is not None))
    if result.method != "none" and full:
        _OCR_CACHE[key] = result
    return result


def _extract_text_uncached(file_path: str, *, ocr: bool = True) -> OcrResult:
    """Extrahiert Text aus einer Quittungs-Datei (Text-Layer zuerst, dann OCR).

    Robust: korrupte/unlesbare Dateien führen nie zu einem Crash, sondern zu
    einem leeren Ergebnis (``method='none'``).

    Mit ``ocr=False`` wird der **teure Tesseract-Fallback übersprungen** — gedacht für
    Listen-/Übersichts-Ansichten, die schnell laden müssen: Text-Layer-PDFs liefern
    weiterhin Betrag/Datum, reine Foto-Belege ergeben dann ``method='none'`` (deren
    OCR übernimmt der schrittweise Hintergrund-Abgleich).
    """
    path = Path(file_path)
    if not path.is_file():
        return OcrResult("", "none", None)

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text = _pdf_text_layer(path)
            # Text-Layer zählt als vollwertig, wenn er lang genug ist ODER einen Betrag
            # trägt: ein maschinell erzeugter Layer ist IMMER verlässlicher als OCR über
            # die gerenderte Seite — auch ein kurzer („Beispielshop / Total CHF 464.27").
            if len(text.strip()) >= _MIN_TEXT_CHARS or extract_amount(text) is not None:
                return OcrResult(text, "text-layer", extract_amount(text), extract_date(text))
            # Nicht (gut) lesbar → OCR-Fallback, falls erlaubt und Tesseract verfügbar.
            if ocr and ocr_available():
                ocr_text = _ocr_pdf(path)
                if ocr_text.strip():
                    return OcrResult(ocr_text, "ocr", extract_amount(ocr_text), extract_date(ocr_text))
            return OcrResult(text, "text-layer" if text.strip() else "none",
                             extract_amount(text), extract_date(text))

        if suffix in IMAGE_EXTENSIONS:
            if ocr and ocr_available():
                ocr_text = _ocr_image(path)
                return OcrResult(ocr_text, "ocr", extract_amount(ocr_text), extract_date(ocr_text))
            return OcrResult("", "none", None)
    except Exception:
        # Korrupte Datei, ungültiges PDF, OCR-Fehler — niemals den Aufrufer abbrechen.
        return OcrResult("", "none", None)

    return OcrResult("", "none", None)


def diagnose_receipt_file(file_path: str) -> str:
    """OCR-Diagnose für eine Datei aus dem Ordner (PDF oder Bild) — zeigt, WARUM kein
    Text erkannt wird (Tesseract da/Version, Bildgrösse, Treffer je Variante). Wird im
    Zuordnen-Assistenten per Button abgerufen; läuft immer durch (nie ein Crash)."""
    path = Path(file_path)
    if not path.is_file():
        return "[OCR-Diagnose] Datei nicht gefunden."
    suffix = path.suffix.lower()
    try:
        Image = pil_image()

        if suffix == ".pdf":
            text = _pdf_text_layer(path)
            head = [f"[OCR-Diagnose] PDF · Text-Layer: {len(text.strip())} Zeichen"]
            if len(text.strip()) >= _MIN_TEXT_CHARS:
                return "\n".join(head + ["→ Text-Layer reicht, kein OCR nötig. Auszug:", text.strip()[:200]])
            doc = fitz.open(path)
            try:
                if len(doc) == 0:
                    return "\n".join(head + ["PDF hat 0 Seiten."])
                pix = _pixmap(doc[0])
                img = Image.open(io.BytesIO(pix.tobytes("png")))
            finally:
                doc.close()
            return "\n".join(head + ["Zu wenig Text-Layer → OCR auf Seite 1:", _ocr_diagnostics(img)])
        return _ocr_diagnostics(Image.open(path))
    except Exception as e:  # noqa: BLE001
        return f"[OCR-Diagnose] Ausnahme: {e!r}"


def extract_text_from_bytes(data: bytes, suffix: str = "") -> OcrResult:
    """Wie :func:`extract_text`, aber aus **rohen Bytes im Speicher** — für den
    mobilen Foto-Upload, bei dem das Bild NIE auf die Platte geschrieben wird.

    Bild → Vorverarbeitung + Tesseract. PDF → Text-Layer, sonst OCR-Fallback.
    Robust: jeder Fehler → leeres Ergebnis (``method='none'``)."""
    suffix = (suffix or "").lower()
    try:
        if suffix == ".pdf":
            doc = fitz.open(stream=data, filetype="pdf")
            try:
                text = "\n".join(page.get_text() for page in doc)
                if len(text.strip()) >= _MIN_TEXT_CHARS or extract_amount(text) is not None:
                    return OcrResult(text, "text-layer", extract_amount(text), extract_date(text))
                if ocr_available():
                    Image = pil_image()
                    parts = []
                    for page in list(doc)[:MAX_OCR_SEITEN]:
                        pix = _pixmap(page)
                        parts.append(_ocr_pil_image(Image.open(io.BytesIO(pix.tobytes("png")))))
                    ocr_text = "\n".join(parts)
                    if ocr_text.strip():
                        return OcrResult(ocr_text, "ocr", extract_amount(ocr_text), extract_date(ocr_text))
                return OcrResult(text, "text-layer" if text.strip() else "none",
                                 extract_amount(text), extract_date(text))
            finally:
                doc.close()

        # Bild (jpg/png/webp/…): braucht eine OCR-Engine (RapidOCR primär).
        Image = pil_image()
        src = Image.open(io.BytesIO(data))
        text = _ocr_pil_image(src) if ocr_available() else ""
        if not text.strip():
            # Kein Text → Diagnose in den Rohtext legen (sichtbar unter „OCR-Rohtext
            # anzeigen"), damit man genau sieht, woran es scheitert statt nur „leer".
            return OcrResult(_ocr_diagnostics(src), "none", None)
        return OcrResult(text, "ocr", extract_amount(text), extract_date(text))
    except Exception:
        return OcrResult("", "none", None)
