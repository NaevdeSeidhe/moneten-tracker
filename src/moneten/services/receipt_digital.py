"""Digitale Quittung: Belegtext → strukturierte Positionen + Kategorien + Lernen.

Kette für den Foto-Upload (und wiederverwendbar für Ordner-Belege):

1. :func:`analyze` macht aus dem OCR-Text eine strukturierte Quittung
   (Händler, Datum, Total, Positionen mit Kategorie). Die Kategorie je Position
   kommt aus — in dieser Reihenfolge — **gelernten Regeln** (siehe
   :class:`ReceiptItemRule`), dem eingebauten Lexikon (``receipt_split``) und den
   Händler-Regeln (``categorization``).
1b. :func:`pruefe_positionen` ist die **Gegenprobe**: die Summe der Positionen
   muss den erkannten Beleg-Total ergeben. Geht sie nicht auf, gilt die Liste
   als ungeprüft — sie wird gezeigt und ist korrigierbar, aber nicht gespeichert.
2. :func:`save_receipt` lernt aus der (bestätigten/korrigierten) Quittung und
   ordnet sie sofort einer passenden Buchung zu (Betrag + Datum) — oder legt sie
   als :class:`PendingReceipt` ab, bis die Bankbuchung auftaucht.
3. :func:`match_pending` ordnet vorgemerkte Belege nachträglich zu (z.B. nach
   einem Bank-Import).

Alles lokal/offline, keine KI — gleiche Philosophie wie die Regel-Engine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Attachment, Category, PendingReceipt, ReceiptItemRule, Transaction
from moneten.services import anhang_tresor, artikelnamen
from moneten.services.categorization import load_active_rules, match_category
from moneten.services.receipt_match import _merchant_tokens, find_match
from moneten.services.receipt_ocr import OcrResult
from moneten.services.receipt_split import (
    _guess_label,
    _resolve_label_category,
    parse_receipt_items_menge,
)


def merchant_key(merchant: str | None) -> str:
    """Kanonischer Händler-Schlüssel (klein, nur a-z0-9) — '' wenn unbekannt."""
    return re.sub(r"[^a-z0-9]", "", (merchant or "").lower())[:120]


# Kopfzeilen, die NICHT der Händlername sind (Bon-ID, Öffnungszeit, Kontakt …).
# Bewusst eng gehalten — „GmbH/AG/Markt" gehören oft zum echten Händlernamen.
# So lang darf eine Kopfzeile hoechstens sein, um noch als Haelfte eines
# umgebrochenen Logos zu gelten.
_KOPF_ANHANG = 24


_MERCHANT_NOISE = re.compile(
    r"\b(bon-?id|kassenbon|lieferdatum|öffnungszeit|oeffnungszeit|telefon|tel\.|"
    r"e-?mail|datum|uhrzeit|filiale|quittung|beleg|rechnung)\b"
    # Anschriftzeile: Postleitzahl mit Ort. Die vier Ziffern muessen ALLEIN
    # stehen — ohne die Schranken griff die Regel die letzten vier Stellen
    # eines EAN-Codes, und ein OBI-Bon („7610000000001  Gruenduengung")
    # verlor damit jede Position.
    # Anschriftzeile: Postleitzahl mit Ort, oder ein Strassenwort. Gemessen an
    # einem Apotheken-Beleg, dessen Kopf „MUSTERSTADT" hiess — der ORT aus der Adresse,
    # weil die Zeile mit dem Ladennamen daneben stand. Ein Ort ist kein Händler,
    # und ein falscher Kopf ist schlimmer als keiner: der Händlername steuert die
    # gelernten Regeln, und eine Regel unter „MUSTERSTADT" träfe jeden Beleg aus dieser Stadt.
    r"|(?<!\d)\d{4}(?!\d)\s*[A-ZÄÖÜ][a-zäöü]{2,}"
    r"|(?:strasse|str\.|weg|platz|gasse|allee)(?![a-zäöü])",
    re.IGNORECASE,
)


# Bekannte CH-Händler: zuverlässig am Markennamen erkannt, AUCH wenn die Kopfzeile durch
# OCR-Rand-Rauschen/Spalten verhunzt ist (z. B. „Ä   ALDI SUISSE AG   fe"). Als Wortgrenze
# gesucht, damit „spar" nicht in „sparen" trifft. Reihenfolge: spezifische zuerst.
_KNOWN_MERCHANTS: list[tuple[str, str]] = [
    ("karma", "Karma"),  # vor „coop": Karma-Bons tragen unten „COOP GENOSSENSCHAFT"
    ("aldi", "Aldi Suisse"), ("migros", "Migros"), ("denner", "Denner"), ("coop", "Coop"),
    ("lidl", "Lidl"), ("manor", "Manor"), ("volg", "Volg"), ("spar", "Spar"),
    ("galaxus", "Galaxus"), ("digitec", "Digitec"), ("microspot", "Microspot"),
    ("brack", "Brack"), ("interio", "Interio"), ("ikea", "Ikea"), ("jumbo", "Jumbo"),
    ("hornbach", "Hornbach"), ("obi", "OBI"), ("landi", "Landi"), ("decathlon", "Decathlon"),
    ("fielmann", "Fielmann"), ("ochsner", "Ochsner Sport"), ("dosenbach", "Dosenbach"),
    ("interdiscount", "Interdiscount"), ("mediamarkt", "MediaMarkt"),
]


def _le1(a: str, b: str) -> bool:
    """Edit-Distanz ≤ 1 (eine Ersetzung/Einfügung/Löschung) — fängt 1-Zeichen-OCR-Fehler
    in Markennamen (z. B. „karmd"/„karna" ↔ „karma")."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    if la > lb:  # b sei das kürzere
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
        else:
            diff += 1
            if diff > 1:
                return False
        j += 1
    return True


def guess_merchant(text: str | None) -> str | None:
    """Schätzt den Händler — zuerst über die Liste bekannter CH-Händler (robust gegen
    OCR-Rand-Rauschen, inkl. 1-Zeichen-Fehler), sonst heuristisch aus den obersten Zeilen.

    Überspringt im Fallback Metadaten-Kopfzeilen (Bon-ID, Öffnungszeit, Datum/Uhrzeit) und
    fasst Spalten-Leerzeichen zusammen, damit eine breit gesetzte Kopfzeile nicht an der
    Längen-Grenze scheitert.
    """
    low = (text or "").lower()
    kandidaten: list[str] = []
    tokens = set(re.findall(r"[a-zäöü]{4,}", low))
    for key, canon in _KNOWN_MERCHANTS:
        if re.search(rf"\b{re.escape(key)}\b", low):
            return canon
        # 1-Zeichen-OCR-Fehler tolerieren (z. B. „karmd" → „karma"); nur bei längeren
        # Marken (≥5), damit kurze Wörter nicht zufällig treffen.
        if len(key) >= 5 and any(_le1(tok, key) for tok in tokens):
            return canon
    for raw in (text or "").splitlines():
        line = re.sub(r"\s{2,}", " ", raw.strip())  # Spalten-Leerzeichen → ein Leerzeichen
        if len(re.sub(r"[^A-Za-zÄÖÜäöü]", "", line)) < 3:
            continue
        if _MERCHANT_NOISE.search(line):
            continue
        if re.search(r"\d{1,2}[.\-/]\d", line) or re.search(r"\b\d{1,2}:\d{2}\b", line):
            continue  # Datum/Uhrzeit → überspringen
        if len(line) > 42:
            continue
        kandidaten.append(line)
        if len(kandidaten) >= 2:
            break
    if not kandidaten:
        return None
    # Ein Logo bricht oft um: „MUSTERSTADT" / „APOTHEKE" sind zwei Zeilen und EIN Name.
    # Gemessen an einem Apotheken-Beleg, dessen Kopf danach „MUSTERSTADT" hiess — also
    # der Ort, nicht der Laden. Zusammengezogen wird nur, wenn die erste Zeile
    # zu kurz ist, um allein ein Name zu sein, und die zweite kurz genug, um zu
    # ihr zu gehoeren; sonst waere jede Adresszeile plotzlich Teil des Namens.
    erste = kandidaten[0]
    # Nicht die LAENGE entscheidet, sondern die Form: EIN Wort, gefolgt von EINEM
    # Wort. So bricht ein Logo um („MUSTERSTADT" / „APOTHEKE"), und so sieht sonst kaum
    # etwas aus — ein ausgeschriebener Firmenname traegt fast immer ein Leerzeichen
    # („Grossverteiler Musterstadt AG") und bleibt damit allein stehen.
    if (len(kandidaten) > 1
            and " " not in erste and " " not in kandidaten[1]
            and len(erste) <= _KOPF_ANHANG and len(kandidaten[1]) <= _KOPF_ANHANG):
        return f"{erste} {kandidaten[1]}"[:160]
    return erste[:160]


# Woerter, die am Anfang eines Positionsnamens NICHTS ueber die Ware sagen:
# Funktionswoerter und Eigenmarken/Label. Sie standen als Stichwort in gelernten
# Regeln, und weil eine Regel auch GENERISCH (ohne Haendler) angelegt wird, schlug
# sie danach bei jedem Haendler zu. Nachgemessen: ein Bon mit "Aus der Region...",
# "M-Classic..." und "Bio..." lernte "aus", "classic", "bio" -- und danach landete
# "Bio Kerze Citronella" bei einem anderen Haendler unter Lebensmitteln statt
# unter Haushalt, weil die gelernte Regel VOR dem eingebauten Lexikon greift.
_KEIN_STICHWORT = frozenset({
    "aus", "der", "die", "das", "dem", "den", "des", "ein", "eine", "einer",
    "fuer", "mit", "und", "von", "vom", "zum", "zur", "per", "pro", "im",
    "bio", "classic", "prix", "garantie", "budget", "selection",
    "naturaplan", "naturafarm", "terrasuisse", "optigal", "farmer", "regional",
    "region", "demeter", "qualite", "label", "nature",
    "migros", "coop", "denner", "aldi", "lidl", "volg", "spar",
})

# Kuerzeste Laenge fuer eine GENERISCHE Regel (ohne Haendler). Kurze Stichwoerter
# sind fast immer Reste eines Labels oder einer Mengenangabe; haendlerspezifisch
# richten sie wenig an, generisch wirken sie auf jeden kuenftigen Beleg.
_GENERISCH_AB = 4


def _item_keyword(name: str) -> str:
    """Normalisiertes Stichwort einer Position (fuer Lern-Regeln).

    Uebersprungen wird, was am Wortanfang nichts ueber die Ware sagt (siehe
    :data:`_KEIN_STICHWORT`) -- Umlaute vorher aufgeloest, weil dieselbe Marke
    mal mit und mal ohne geschrieben wird. Bleibt nichts uebrig, gilt wieder das
    erste Wort: ein Beleg, auf dem nur "Bio" steht, soll ein Stichwort bekommen,
    nur eben eines, das :data:`_GENERISCH_AB` noch von der generischen Regel
    fernhaelt.
    """
    low = re.sub(r"\b\d+[.,]?\d*\s*(x|kg|g|ml|dl|cl|l|stk)?\b", " ", name.lower())
    toks = re.findall(r"[a-z\u00e4\u00f6\u00fc]{3,}", low)
    tragend = [w for w in toks if _ohne_umlaute(w) not in _KEIN_STICHWORT]
    if tragend:
        return tragend[0]
    return toks[0] if toks else low.strip()[:40]


def _ohne_umlaute(wort: str) -> str:
    """ae/oe/ue/ss statt Umlauten -- nur fuer den Vergleich mit der Sperrliste."""
    for a, b in (("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"), ("\u00df", "ss"),
                 ("\u00e9", "e"), ("\u00e8", "e")):
        wort = wort.replace(a, b)
    return wort


def _to_decimal(raw) -> Decimal | None:
    """Betrag aus einem Formularfeld — ``None``, wenn es keiner ist.

    ``is_finite()`` ist Pflicht und keine Vorsicht: ``Decimal("NaN")`` entsteht
    ohne Ausnahme, und ``quantize`` wirft dafür ebenfalls keine. Der Wert kommt
    ungeprüft aus dem Browser; ein „NaN" im Preisfeld riss damit erst weiter
    unten beim Vergleich ``abs(summe - total)`` eine ``InvalidOperation`` —
    also einen 500er mitten im Speichern eines Belegs. „Infinity" hätte
    dieselbe Wirkung.
    """
    try:
        wert = Decimal(str(raw)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return wert if wert.is_finite() else None


# ---------------------------------------------------------------------------
# Gegenprobe: ergeben die erkannten Positionen den erkannten Beleg-Total?
# ---------------------------------------------------------------------------
#
# Der Grund für diese Probe ist ein Vorfall: eine um eine Zeile versetzte
# Positionsliste wurde als Vorschlag angeboten, und nichts hat es gemerkt. Jede
# einzelne Zeile sah für sich plausibel aus — erst die Summe verrät den Versatz.
# Beim Rechnungs-Parser gibt es dieselbe Probe seit längerem
# (:func:`~moneten.services.belege_parser.rechnung_nach_profil`); dort hat sie zwei
# echte Lesefehler gefunden, die sonst als Zahl in einer Kurve gestanden hätten.

#: Grösste Abweichung, die ein RICHTIG gelesener Kassenbon haben darf.
#:
#: Sie kommt aus der Rappenrundung und nicht aus dem Wunsch, dass es aufgeht:
#: Barzahlungen werden in der Schweiz auf 5 Rappen gerundet, die Positionen
#: stehen aber in ganzen Rappen. Der Rest der Positions-Summe modulo 5 Rappen
#: ist also 0, 1, 2, 3 oder 4 Rappen und wird auf 0 oder 5 gezogen — mehr als
#: **2 Rappen** kann dabei nicht entstehen. Gerundet wird einmal, auf den Total;
#: die Toleranz wächst deshalb nicht mit der Zahl der Positionen.
#:
#: Der Preis dieser Toleranz ist benannt und klein: ein Lesefehler von einem
#: oder zwei Rappen an einem einzelnen Preis rutscht durch. Alles Grössere —
#: eine übersehene Position, ein Zahlendreher, ein Zeilenversatz — nicht.
# Grenzen einer einzelnen Position. Dieselbe Spanne, die der Zeilen-Parser
# schon anwendet (services/receipt_split.py) — der Weg über das Formular
# hatte sie nicht, und genau dort kommen die Werte aus dem Browser.
_PREIS_MIN = Decimal("0.01")
_PREIS_MAX = Decimal("10000")

RUNDUNGS_TOLERANZ = Decimal("0.02")

# Die ausgewiesene Rundungszeile des Bons wird bewusst NICHT gelesen, obwohl der
# Rechnungs-Parser das tut. Dort steht „Rundungsdifferenz" mit eindeutigem
# Vorzeichen; auf Kassenbons heisst dieselbe Sache mal „Rundung -0.02", mal
# „Rundungsvorteil 0.02" — gleiche Aussage, entgegengesetztes Vorzeichen. Ein
# geratenes Vorzeichen liesse korrekte Bons durchfallen und falsche bestehen.
# Die Obergrenze aus der Rundungsregel ist die verlässlichere Aussage.

#: Zeilen, die eine ERSPARNIS ausweisen („Sie sparen total", „Ihr Vorteil").
#: Sie stehen neben dem Total und sind eine Auskunft, kein Einkauf. Mitgezählt
#: wäre der Bon um genau diesen Betrag zu teuer — die Probe schlüge fehl, obwohl
#: alle echten Positionen stimmen. Im Preisverlauf stünde zudem ein „Artikel"
#: namens „Sie sparen". Die meisten dieser Zeilen fängt schon der Zeilen-Parser
#: ab (sie tragen „total" oder „CHF"); die ohne beides landen hier.
#:
#: Die Wortgrenzen sind nicht Kosmetik: ohne die hintere verschwände „Ihr
#: Vorteilspack" aus der Positionsliste — ein Produkt, das der Nutzer gekauft hat.
_ERSPARNIS = re.compile(
    r"\b(sie\s*sparen|gespart|ersparnis|ihr\s*vorteil)\b",
    re.IGNORECASE,
)

# Rabatte, die schon im Zeilen-Total stecken, werden NICHT eigens abgezogen.
# Auf Tabellen-Bons (Menge | Preis | Aktion | Total) nimmt der Zeilen-Parser die
# rechteste Spalte, und die ist bereits der Nettobetrag der Zeile. Ein zweiter
# Abzug machte aus einem stimmigen Bon einen unstimmigen. Steht ein Rabatt
# dagegen als eigene Zeile mit Minuszeichen, verliert der Zeilen-Parser das
# Vorzeichen und die Summe fällt zu hoch aus — dann schlägt die Probe fehl, und
# das ist richtig so: die Liste behauptete einen Einkauf, den es nicht gab.


@dataclass(frozen=True)
class Gegenprobe:
    """Ergebnis der Positions-Probe. ``ok`` heisst: die Liste darf gelten.

    Bewusst ohne Meldungstext — die Oberfläche rechnet die Probe bei jeder
    Korrektur neu und formuliert dabei selbst. Ein hier gebauter Satz wäre nach
    der ersten Änderung des Nutzers falsch.
    """

    ok: bool
    summe: Decimal
    total: Decimal | None

    @property
    def abweichung(self) -> Decimal | None:
        """Summe minus Total — ``None``, wenn kein Total erkannt wurde."""
        return None if self.total is None else self.summe - self.total


def ist_ersparnis(name: str) -> bool:
    """True, wenn die Zeile eine ausgewiesene Ersparnis ist statt einer Position."""
    return bool(_ERSPARNIS.search(name or ""))


def pruefe_positionen(items: list[dict], total: Decimal | None) -> tuple[list[dict], Gegenprobe]:
    """Auskunftszeilen aussortieren, Rest gegen den Beleg-Total prüfen.

    Rückgabe: ``(bereinigte Positionen, Gegenprobe)``.

    Ohne erkannten Total gibt es nichts zu prüfen — die Liste gilt dann als
    ungeprüft, nicht als richtig. Dasselbe für eine leere Liste: „nichts
    erkannt" ist keine bestandene Probe.

    Eine Position, deren Preis sich nicht lesen lässt, lässt die Probe
    ausdrücklich fehlschlagen. Sie einfach zu überspringen wäre die gefährlichere
    Variante: die restlichen Zeilen könnten den Total zufällig genau ergeben, und
    die Liste sähe geprüft aus, obwohl eine Position unbekannt ist.
    """
    bereinigt = [it for it in items if not ist_ersparnis(it.get("name", ""))]
    preise = [_to_decimal(it.get("price")) for it in bereinigt]
    lesbar = all(p is not None for p in preise)
    summe = sum((p for p in preise if p is not None), Decimal("0"))
    # Jede Position muss FÜR SICH plausibel sein, nicht nur die Summe. Ein
    # reiner Summentest lässt sich mit zwei Fehlern austricksen, die sich
    # aufheben: nachgemessen gingen {−20.00, +65.60} gegen einen Total von
    # 45.60 als „geprüft" durch, und der negative „Preis" landete danach im
    # Preisverlauf. Ein Kassenbon hat keine negativen Positionen — ein Rabatt
    # steckt im Zeilen-Total, und eine Rückgabe ist ein eigener Beleg.
    plausibel = all(p is not None and _PREIS_MIN <= p <= _PREIS_MAX for p in preise)
    ok = (bool(bereinigt) and total is not None and lesbar and plausibel
          and abs(summe - total) <= RUNDUNGS_TOLERANZ)
    return bereinigt, Gegenprobe(ok=ok, summe=summe, total=total)


def categorize_item(name: str, *, learned, merchant_k, cats, pairs, fallback=None):
    """Kategorie für eine Position: gelernt → Lexikon → Händler-Regel → Fallback."""
    low = name.lower()
    toks = set(re.findall(r"[a-zäöü]{3,}", low))
    # 1) Gelernt — händlerspezifisch zuerst, dann generisch ("").
    for want in (([merchant_k] if merchant_k else []) + [""]):
        for kw, mk, cid in learned:
            if mk == want and (kw in toks or kw in low):
                return cid
    # 2) Eingebautes Lexikon (Lebensmittel/Alkohol/Haushalt/Drogerie/Genuss).
    label, _ = _guess_label(name, pairs)
    if label:
        cid = _resolve_label_category(label, cats)
        if cid is not None:
            return cid
    # 3) Händler-Regeln (z.B. „migros" → Lebensmittel).
    cid = match_category(pairs, name)
    if cid is not None:
        return cid
    return fallback


def analyze(db: Session, ocr: OcrResult) -> dict:
    """OCR-Ergebnis → strukturierte digitale Quittung (Dict, JSON-serialisierbar).

    ``positions_ok`` sagt, ob die Positionen die Gegenprobe gegen den Total
    bestehen. Sie werden auch bei ``False`` mitgeliefert — der Nutzer soll sehen
    und korrigieren können, was gelesen wurde. Was er nicht bekommt, ist eine
    Liste, die sich als geprüft ausgibt; und gespeichert wird sie erst, wenn sie
    aufgeht (:func:`save_receipt`).
    """
    text = ocr.text or ""
    merchant = guess_merchant(text)
    mk = merchant_key(merchant)
    cats = list(db.scalars(select(Category)))
    cat_name = {c.id: c.name for c in cats}
    pairs = load_active_rules(db)
    learned = [(r.keyword, r.merchant_key or "", r.category_id) for r in db.scalars(select(ReceiptItemRule))]
    alias = artikelnamen.alias_karte(db)
    # Die Kategorie, die der LADEN nahelegt („Migros" → Lebensmittel). Sie ist
    # der Rückfall für Positionen, die weder gelernt noch im Lexikon stehen.
    #
    # Gemessen an einem Migros-Bon mit neun Zeilen: drei blieben ohne Kategorie
    # und mussten von Hand gesetzt werden. Ein Beleg-Posten ohne Kategorie
    # verfälscht zwar kein Budget (Aufteilungen entstehen dort nicht automatisch),
    # aber er kostet bei jedem Einkauf dieselben Handgriffe.
    haendler_kat = match_category(pairs, merchant or "")

    items = []
    for name, price, menge in parse_receipt_items_menge(text):
        # Bestaetigte Schreibweise VOR allem anderen: die Kategorie soll zum
        # richtigen Namen gesucht werden, nicht zum verlesenen. ``name_ocr``
        # reist mit, damit beim Speichern zu sehen ist, was korrigiert wurde.
        gelesen = name
        name = artikelnamen.anwenden(name, alias)
        cid = categorize_item(name, learned=learned, merchant_k=mk, cats=cats, pairs=pairs)
        # Der Rückfall wird MITGESCHRIEBEN, nicht nur angewendet: nur so lässt
        # sich beim Speichern sagen, ob die Kategorie bestätigt wurde oder
        # ob sie bloss vom Laden geerbt wurde (siehe :func:`learn`).
        auto = None
        if cid is None and haendler_kat is not None:
            cid = auto = haendler_kat
        items.append({
            "name": name, "price": str(price),
            # Was die Erkennung WIRKLICH gelesen hat. Bleibt am Eintrag, damit
            # beim Speichern zu sehen ist, welche Korrektur gemacht wurde —
            # daraus lernt die App die Schreibweise (services/artikelnamen).
            "name_ocr": gelesen,
            # Stückzahl mitschreiben: der Preis ist das Positions-Total, und der
            # Anzeigename hat die Menge nicht mehr (``_clean_name`` entfernt sie).
            # Ohne dieses Feld liesse sich der Stückpreis später nicht mehr bilden.
            "qty": menge,
            "category_id": cid, "category_name": cat_name.get(cid),
            # Welche Kategorie vom Laden geerbt wurde — leer, wenn sie aus dem
            # Namen kommt. Wird beim Lernen gebraucht, siehe :func:`learn`.
            "category_auto_id": auto,
        })
    items, probe = pruefe_positionen(items, ocr.amount)
    return {
        "merchant": merchant or "",
        "merchant_key": mk,
        "date": ocr.date.isoformat() if ocr.date else None,
        "amount": str(ocr.amount) if ocr.amount is not None else None,
        "method": ocr.method,
        "items": items,
        "positions_ok": probe.ok,
        # Die Toleranz reist mit, damit die Oberfläche bei jeder Korrektur nach
        # DERSELBEN Regel neu prüft. Zwei Zahlen an zwei Orten wären zwei Regeln,
        # sobald eine davon angefasst wird.
        "positions_toleranz": str(RUNDUNGS_TOLERANZ),
    }


def _upsert_item_rule(db: Session, keyword: str, merchant_key: str, category_id: int) -> None:
    """Lern-Regel (keyword, merchant_key) anlegen oder Kategorie aktualisieren."""
    existing = db.scalar(
        select(ReceiptItemRule).where(
            ReceiptItemRule.keyword == keyword, ReceiptItemRule.merchant_key == merchant_key
        )
    )
    if existing is not None:
        existing.category_id = category_id
    else:
        db.add(ReceiptItemRule(keyword=keyword, merchant_key=merchant_key, category_id=category_id))
        # Sofort flushen: bei autoflush=False sähe ein zweiter Aufruf mit gleichem
        # (keyword, merchant_key) — z. B. zwei Bon-Positionen „Nussbrot …" → Stichwort
        # „nussbrot" — diesen Datensatz sonst nicht und würde ein zweites INSERT erzeugen,
        # das beim Commit an der UNIQUE-Constraint scheitert (Beleg liesse sich nicht speichern).
        db.flush()


def _nur_scheinbar_geprueft(structured: dict) -> bool:
    """Wahr, wenn die Gegenprobe an dieser Quittung gar nichts beweisen konnte.

    Genau eine Position, deren Preis der Total IST: dann ist ``Summe == Total``
    keine Probe, sondern eine Identitaet — sie geht auch dann auf, wenn die
    einzige gefundene „Position" in Wahrheit die Totalzeile war. Der Fall ist
    nicht erfunden: ein Kinobeleg lieferte als einzige Zeile „Total bezahlt
    (Karte)". :data:`receipt_split._SKIP` kennt diese Schreibung inzwischen,
    aber die Liste kann nie vollstaendig sein — irgendein Laden schreibt es
    wieder anders.

    Folge ist bewusst NUR: nicht lernen. Gespeichert wird die Position weiter,
    dort ist sie sichtbar und lässt sich richtigstellen. Eine gelernte Regel
    dagegen wirkt still auf jeden kuenftigen Beleg jedes Haendlers.

    Der Preis: bei einem echten Ein-Positionen-Beleg muss die Kategorie einmal
    mehr von Hand kommen. Das ist die guenstigere Seite des Irrtums.
    """
    items = [it for it in structured.get("items") or []
             if not ist_ersparnis(it.get("name", ""))]
    if len(items) != 1:
        return False
    preis = _to_decimal(items[0].get("price"))
    total = _to_decimal(structured.get("amount"))
    return preis is not None and total is not None and abs(preis - total) <= RUNDUNGS_TOLERANZ


def learn(db: Session, structured: dict) -> int:
    """Speichert/aktualisiert Lern-Regeln aus den (bestätigten) Positionen — **händler-
    spezifisch UND generisch** (mk=""). Die generische Regel greift, falls der Händler beim
    nächsten Beleg anders/nicht erkannt wird (z. B. Karma mal als „Karma", mal als „Coop"),
    so dass eine einmal vergebene Kategorie zuverlässig wieder vorgeschlagen wird."""
    mk = structured.get("merchant_key") or ""
    if _nur_scheinbar_geprueft(structured):
        return 0
    n = 0
    for it in structured.get("items", []):
        cid = it.get("category_id")
        if not cid:
            continue
        # Vom Laden geerbt und unverändert gelassen: das ist keine Aussage über
        # DIESEN Artikel, sondern über den Laden. Daraus eine Regel zu lernen
        # hiesse, jeden unbekannten Migros-Artikel für immer als Lebensmittel zu
        # führen — auch die Kerze und das Waschmittel. Wer die Kategorie ändert,
        # sagt etwas über den Artikel; daraus wird gelernt.
        if cid == it.get("category_auto_id"):
            continue
        kw = _item_keyword(it.get("name", ""))
        if not kw:
            continue
        # Haendlerspezifisch immer; generisch nur ab einer Laenge, die kein Rest
        # eines Labels mehr sein kann. Die generische Regel ist das scharfe
        # Werkzeug: sie wirkt auf jeden kuenftigen Beleg jedes Haendlers und
        # schlaegt dabei das eingebaute Lexikon.
        ziele = {mk} if len(kw) < _GENERISCH_AB else {mk, ""}
        for m in ziele:
            _upsert_item_rule(db, kw, m, cid)
        n += 1
    return n


def _attach_structured(db: Session, tx: Transaction, structured: dict, ocr_text: str | None) -> Attachment:
    """Hängt die digitale Quittung (nur Daten, kein Bild) an eine Buchung."""
    att = Attachment(
        transaction_id=tx.id,
        file_path=None,
        original_name=(structured.get("merchant") or "Beleg"),
        ocr_text=ocr_text or None,
        parsed_items_json=json.dumps(structured, ensure_ascii=False),
    )
    db.add(att)
    return att


def _try_match(
    db: Session, amount: Decimal | None, date_guess: date | None,
    *, merchant_tokens: tuple[str, ...] = (),
) -> Transaction | None:
    """Auto-Zuordnung für Foto-/vorgemerkte Belege — dieselbe robuste Logik wie bei
    Ordner-Belegen (Tier 1 + Händler + Tier 2, siehe :func:`find_match`). Der Betrag ist
    OCR-gelesen → ``reliable_amount=False`` (Tier 2 greift nur mit Händler-Bestätigung)."""
    return find_match(db, amount, date_guess, merchant_tokens=merchant_tokens, reliable_amount=False)


def _gepruefte_quittung(structured: dict) -> tuple[dict, int]:
    """Kopie der Quittung, deren Positionen die Gegenprobe bestanden haben.

    Geht die Probe nicht auf, bleiben ``items`` leer: was nicht aufgeht, wird
    weder gelernt noch als Beleg-Aufteilung abgelegt noch in den Preisverlauf
    gespeist. Total, Händler, Datum und der OCR-Rohtext bleiben — die stehen
    gross und allein auf dem Bon und sind auch dann verlässlich. Die Buchung
    lässt sich also anlegen, nur eben ohne Aufteilung.

    Die Probe läuft hier ein zweites Mal, obwohl die Oberfläche sie schon zeigt:
    was gespeichert wird, entscheidet nicht die Anzeige. Gelernte Regeln wirken
    auf jeden künftigen Beleg — eine falsche Position bliebe sonst dauerhaft.

    **Was sie NICHT beweist.** Sie prüft die Summe und die Spanne jeder Position,
    nicht die Zuordnung von Name zu Preis. Zwei vertauschte Preise gehen auf,
    und der Preisverlauf führt sie danach am falschen Artikel. Bestanden heisst
    hier also „nicht offensichtlich falsch", nicht „richtig gelesen" — der
    strengere Fall, ein Bon mit genau einer Position, wird darum trotz
    bestandener Probe nicht gelernt (siehe :func:`_nur_scheinbar_geprueft`).

    Rückgabe: ``(Quittung, Zahl der verworfenen Positionen)``.
    """
    items = list(structured.get("items") or [])
    bereinigt, probe = pruefe_positionen(items, _to_decimal(structured.get("amount")))
    out = dict(structured)
    out["items"] = bereinigt if probe.ok else []
    out["positions_ok"] = probe.ok
    return out, len(bereinigt) if not probe.ok else 0


def save_receipt(db: Session, structured: dict, ocr_text: str | None,
                 *, source: str = "photo", image_path: str | None = None) -> dict:
    """Lernt aus der Quittung und ordnet sie zu — oder merkt sie vor.

    Rückgabe: ``{"attached_tx_id": int|None, "pending_id": int|None,
    "positions_ok": bool, "verworfene_positionen": int}``.
    """
    # Schreibweisen VOR der Gegenprobe lernen — und aus den urspruenglichen
    # Positionen. Die Probe leert `items`, wenn die Summe nicht aufgeht, und
    # genau die Belege brauchen die Korrektur am dringendsten: der Apotheken-Bon,
    # bei dem eine Zeile falsch gelesen wurde, geht nie auf. Danach zu lernen
    # hiesse, nur von den Belegen zu lernen, die ohnehin stimmen.
    #
    # Der Name haengt nicht am Betrag: dass „Perenterol" richtig heisst, bleibt
    # wahr, auch wenn die Summe der Positionen den Total verfehlt.
    for eintrag in structured.get("items") or []:
        artikelnamen.lerne(db, eintrag.get("name_ocr") or "", eintrag.get("name") or "")

    structured, verworfen = _gepruefte_quittung(structured)
    learn(db, structured)
    amount = _to_decimal(structured.get("amount"))
    d = None
    if structured.get("date"):
        try:
            d = date.fromisoformat(structured["date"])
        except (ValueError, TypeError):
            d = None
    tokens = _merchant_tokens(structured.get("merchant") or "", ocr_text or "")
    befund = {"positions_ok": structured["positions_ok"], "verworfene_positionen": verworfen}
    tx = _try_match(db, amount, d, merchant_tokens=tokens)
    if tx is not None:
        _attach_structured(db, tx, structured, ocr_text)
        db.commit()
        return {"attached_tx_id": tx.id, "pending_id": None, **befund}
    pend = PendingReceipt(
        merchant=structured.get("merchant") or None,
        receipt_date=d, amount=amount,
        ocr_text=ocr_text or None,
        items_json=json.dumps(structured, ensure_ascii=False),
        source=source, image_path=image_path,
    )
    db.add(pend)
    db.commit()
    return {"attached_tx_id": None, "pending_id": pend.id, **befund}


def match_pending(db: Session) -> int:
    """Ordnet vorgemerkte Belege nachträglich zu (z.B. nach Bank-Import). Anzahl."""
    n = 0
    for pend in list(db.scalars(select(PendingReceipt))):
        tokens = _merchant_tokens(pend.merchant or "", pend.ocr_text or "")
        tx = _try_match(db, pend.amount, pend.receipt_date, merchant_tokens=tokens)
        if tx is None:
            continue
        try:
            structured = json.loads(pend.items_json) if pend.items_json else {}
        except (ValueError, TypeError):
            structured = {}
        _attach_structured(db, tx, structured, pend.ocr_text)
        # Das Bild geht mit. Es hat ab hier keinen Verweis mehr — was bleibt,
        # ist ein Kassenzettel im Dateisystem, den niemand mehr findet und
        # niemand mehr braucht.
        anhang_tresor.entfernen(pend.image_path)
        db.delete(pend)
        n += 1
    if n:
        db.commit()
    return n
