"""Auto-Split-Vorschlag aus einem Belegtext.

Versucht, die Positionen einer Quittung (Name + Preis je Zeile) zu erkennen, je
Position eine Kategorie zu schätzen und gleichartige Positionen zu einer
Aufteilung zusammenzufassen. Das Ergebnis ist ausdrücklich ein **Vorschlag** —
der Nutzer prüft und korrigiert ihn im Editor, bevor er speichert.

Datenschutz/Offline: rein lokal. Die Positions-Erkennung nutzt (1) ein kleines
eingebautes Stichwort-Lexikon für typische Schweizer Einkäufe (Lebensmittel/
Alkohol/Haushalt/Drogerie/Genuss) und (2) die bestehenden Händler-Regeln. Was
nicht zugeordnet werden kann, fällt auf die bisherige Kategorie der Buchung
zurück. Keine externen Aufrufe, keine KI.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Category, Transaction
from moneten.services.categorization import load_active_rules, match_category

# Preis-Token (Geldbetrag) irgendwo in der Zeile: 12.50 / 1'234,00 / 3.95. Tabellen-
# Belege (Migros: Menge|Preis|Aktion|Total) haben MEHRERE Beträge je Zeile plus OCR-
# Müll am Ende („0", „|") — deshalb suchen wir alle Tokens und nehmen das rechteste
# (= Positions-Total), statt nur am Zeilenende zu schauen.
# Das ``(?!\s*%)`` schliesst Prozentsätze aus: Belege mit MwSt-Spalte rechts vom Preis
# („9.50 CHF   MwSt 2.60 %") gaben sonst den STEUERSATZ als Positions-Total aus.
# Preis in einer Belegzeile. Dieselben drei Nachbedingungen wie ``receipt_ocr._AMOUNT``
# — dort steht die Begründung ausführlich. Kurz: genau zwei Nachkommastellen,
# nicht Teil eines Datums, kein Prozentsatz.
_PRICE_TOKEN = re.compile(r"\d+(?:['’ ]\d{3})*[.,]\d{2}(?!\d)(?!\.\d)(?!\s*%)")

# Uhrzeit (8:00 / 10:40:33) — damit Zeiten/Öffnungszeiten nicht als Preis gelten.
# OHNE Wortgrenzen: OCR klebt die Zeit an die Nachbarspalte, und genau dann wird
# sie gefährlich. Gemessen an einer Fusszeile der Form „05.03.202609:1500112233
# Kasse 1/4" (Datum, Uhrzeit und Bonnummer aneinandergeklebt) — die Zeit war da,
# die Sperre griff nicht, und die Zeile lieferte das Datum als Preis.
_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")
# Zeile ist (fast) nur eine lange Ziffernfolge: EAN/Artikel-/Barcode-/Bon-Nummer.
_NUM_LINE = re.compile(r"^[\s\d'’.\-]{6,}$")
# Führende Artikel-/EAN-Nummer im Namen (≥5 Ziffern) entfernen (z. B. 89373, 537053, EAN-13).
_LEAD_EAN = re.compile(r"^\s*\d{5,}\s*")
# So viele Zeilen darf ein Positionsname überspannen. Drei, weil Gastro-/Kinobelege
# einen Namen über zwei bis drei Zeilen umbrechen; mehr wäre kein Name mehr, sondern
# der halbe Beleg-Kopf.
_NAME_ZEILEN = 3

# Zeilen, die keine Position sind: Summen/Zahlart + Beleg-Kopf/-Fuss
# (Adresse, Telefon, Mail, Bon-ID, Öffnungszeit, Karten-/Zahldetails …).
# Währung und Steuer stehen NICHT hier, sondern in ``_EINHEIT_KOPF`` — siehe dort.
_SKIP = re.compile(
    r"\b(total|summe|subtotal|zwi\s*schens?umme|endsumme|rundung\w*|artikel|"
    r"kartenzahlung|kartenzahl|karte|"
    r"kreditkarte|debit|ma[e3]stro|master\s?card|postfinance|amex|american\s?express|"
    r"twint|bar(?:geld|zahlung)?|r[uü]ckgeld|"
    r"betrag|(?:end|zahl|rechnungs|schluss|gesamt)betrag|gesamt\w*|"
    # „Total bezahlt (Karte): 13.40 CHF" — nachgemessen an einem Kinobeleg, der
    # GENAU eine Zeile hergab, und zwar diese. Die Gegenprobe ging dann auf,
    # weil die eine „Position" der Total war: Summe = Total ist da eine
    # Identitaet, keine Probe. Der Beleg galt als geprueft, wurde gespeichert
    # UND gelernt. „zu zahlen" stand hier schon, „zu bezahlen" traf es nicht.
    r"bezahlt|zu\s*be?zahlen|"
    # „Sie wurden beraten von Frau …", „bedient von …", „Kasse 1/13". Kein
    # Artikel — aber ohne Sperre wurde der Satz zum Positionsnamen, weil die
    # naechste Zeile einen Betrag trug.
    r"ber(?:aten|ater(?:in)?)|bedient|kassiert|kasse\s*\d|"
    r"zu\s*zahlen|saldo|trinkgeld|gegeben|retour|cash|change|punkte|cumulus|"
    r"supercard|filiale|markt|datum|uhrzeit|öffnungszeit|oeffnungszeit|offnungszeit|telefon|tel\.|"
    r"e-?mail|bon-?id|lieferdatum|kassenbon|\bpan\b|eingabemodus|zahlungsmethode|"
    r"payment|terminal|autoris|trace|quittung|beleg|kundenbeleg|rechnung|strasse|str\.|preis|"
    r"genossenschaft|x{6,}\w*)\b",
    re.IGNORECASE,
)

# OCR klebt Zahlen oft DIREKT an Schlüsselwörter („Termina1CHF", „Umsatz8.1%") — dann
# greift ``\b`` nicht, weil Ziffern Wortzeichen sind. Für Summen-/Zahlwörter gelten
# deshalb zusätzlich BUCHSTABEN-Grenzen (links/rechts alles ausser einem Buchstaben
# erlaubt); „terminal" tolerant für l→1/I/| (realer Bug: eine Terminal-Zeile der
# Form „Termina1CHF <Betrag>" wurde als Position gelesen).
_SKIP_GLUED = re.compile(
    r"(?<![a-zäöü])(total|subtotal|summe|umsatz|termina[l1i|]|"
    r"gesamt|bezahlt|(?:end|zahl|rechnungs|schluss|gesamt)?betrag|"
    r"saldo|netto|brutto)(?![a-zäöü])",
    re.IGNORECASE,
)

# Währung und Steuer — geprüft wird damit NUR der Zeilenkopf (alles links vom ersten
# Geldbetrag). Rechts vom Preis sind es Spaltenköpfe: ein Kinobeleg schreibt jede
# Position als „<Preis> CHF   MwSt <Satz> %", und über die ganze Zeile geprüft fiel
# damit jede einzelne Position raus — der Beleg ergab GAR KEINE Positionen. Links vom
# Preis bleiben sie das, was sie waren: die Marke einer Summen-/Steuerzeile.
# Zwei Schreibungen, weil OCR die Einheit ans Wort davor klebt:
#   (a) als eigenes Wort, auch an Ziffern klebend („CHF 8.40", „Termina1CHF") —
#       Buchstaben-Grenzen wie oben, Gross-/Kleinschreibung egal;
#   (b) an einem Kleinbuchstaben klebend, dann aber mit grossem Anfang („TotalCHF 20.20",
#       „TotalMwSt"). Der Wechsel klein→GROSS ist die Klebestelle. Ohne diese
#       Einschränkung risse „Milchflasche" (…chf…) eine echte Position weg.
_EINHEIT_KOPF = re.compile(
    # `mw\s?st`: „MwSt" traegt selbst ein Binnenversal, und die Naht-Trennung
    # macht daraus „Mw St". Ohne die optionale Luecke findet nach dem
    # Entkleben keine Fassung mehr etwas — gemessen an „MwStSatz 1.20".
    r"(?<![a-zäöü])(?:chf|sfr|mw\s?st|vat|tax)(?![a-zäöü])"
    r"|(?-i:[a-zäöü](?:CHF|SFR|MWST|MwSt|VAT|TAX))",
    re.IGNORECASE,
)

# Kartenmarken, tolerant gegen OCR-Verwechslungen und ohne Wortgrenze am Ende —
# beides nachgemessen noetig:
#   „UlSA 8.40"  — grosses I gelesen als kleines L (sieht identisch aus)
#   „V1SA 8.40"  — als Eins gelesen
#   „VISA8.40"   — am Betrag klebend, dadurch greift \b nicht
# Ohne diesen Filter landet die Zahlart als „Artikel" im Preisverlauf, mit dem
# Rechnungstotal als „Preis" — und weil das Total je Beleg schwankt, sieht es
# nach einem Preisverlauf aus.
# ``w`` steht neben ``v`` und ``u``: OCR verwechselt genau diese drei. Gemessen
# an einem Apotheken-Beleg, der als Position „Wisa FinaTta Pay" hergab — die
# Zahlart, mit dem Rechnungstotal als Preis. Die Zeile bestand aus lauter
# gelesenen Zeichen; nur das erste war falsch.
_SKIP_KARTE = re.compile(
    # `\s?` bei den zweiteiligen Marken: sie tragen ein Binnenversal
    # („PostFinanceCard", „MasterCardZahlung"), und die Naht-Trennung macht
    # daraus „Post Finance Card". Ohne das optionale Leerzeichen findet
    # WEDER die Originalfassung (Buchstabe dahinter) NOCH die entklebte etwas.
    r"(?<![a-zäöü])(?:[vuw][i1l]sa|ma[e3]s?tro|master\s?card|post\s?finance|amex)(?![a-zäöü])",
    re.IGNORECASE,
)

# OCR zieht Woerter zusammen: aus „Wisa FinaTta Pay" wurde beim zweiten Scan
# desselben Belegs „WisaFinaTba Pay". Damit greift KEIN Wortgrenzen-Waechter
# mehr — hinter „Wisa" steht ein Buchstabe, und `(?![a-zäöü])` schlaegt unter
# `IGNORECASE` auch bei einem GROSSbuchstaben zu. Die Zahlart landete wieder als
# Position im Beleg, mit dem Rechnungstotal als Preis.
#
# Statt den Waechter aufzuweichen, wird die Zusammenziehung rueckgaengig
# gemacht: ein Wechsel von klein nach GROSS ist im Deutschen keine Wortmitte,
# sondern eine Naht. Das gilt fuer JEDE Marke, nicht nur fuer Visa.
#
# Bewusst NUR fuer die Skip-Pruefung und nicht am Artikelnamen: der heisst
# vielleicht wirklich „iPhone" oder „VitaBasic", und ein Leerzeichen mitten
# darin waere eine zweite Schreibweise im Preisverlauf.
#
# Und bewusst nur an dieser Naht: „VISAGE" bleibt unangetastet (kein Wechsel),
# und „Nivea Visage" ebenso — dort gehoert das „ge" zum selben Wort. Beide
# waeren echte Artikel, und beide wuerden von einer laxeren Regel gefressen.
_NAHT = re.compile(r"(?<=[a-zäöü])(?=[A-ZÄÖÜ])")


def _entklebt(text: str) -> str:
    """Trennt, was OCR ohne Leerzeichen aneinandergehaengt hat."""
    return _NAHT.sub(" ", text)


# Woerter, die auf einem Bon nie einen Artikel benennen. Bewusst KNAPP: die
# Liste entscheidet unten nur zusammen mit „ALLE Woerter der Zeile stehen drin".
_KEIN_ARTIKELWORT = frozenset((
    "total", "summe", "subtotal", "zwischensumme", "endsumme", "endbetrag",
    "schlussbetrag", "bezahlt", "zahlen", "zahlung", "betrag", "saldo", "rundung",
    "rueckgeld", "ruckgeld", "bar", "bargeld", "barzahlung", "gegeben", "retour",
    "change", "cash", "karte", "kartenzahlung", "kreditkarte", "debit", "maestro",
    "mastercard", "postfinance", "amex", "twint", "mwst", "vat", "tax", "chf", "sfr",
))


def _nur_bonwoerter(kopf: str) -> bool:
    """Besteht der Namensteil AUSSCHLIESSLICH aus Bon-Vokabular?

    Der Anlass ist dieselbe Naht wie bei der Zahlart: „Total bezahlt" kann als
    „TotalBezahlt" ankommen, und dann greift `_SKIP` nicht mehr — eine
    Summenzeile wird zur Position, und die Gegenprobe geht tautologisch auf.

    `_SKIP` einfach auf die entklebte Zeile loszulassen waere hier FALSCH: dort
    stehen breite Woerter wie „total", „karte", „markt", „preis". Aus
    „TotalCare Zahnpasta" wuerde „Total Care Zahnpasta", und ein echter Artikel
    verschwaende. Deshalb die strengere Bedingung: es wird nur uebersprungen,
    wenn JEDES Wort der Zeile Bon-Vokabular ist. Ein einziges unbekanntes Wort
    — und sei es „Care" — laesst die Zeile durch.
    """
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", _entklebt(kopf))
    if not woerter:
        return False
    return all(w.lower().replace("ü", "u").replace("ä", "a").replace("ö", "o")
               in _KEIN_ARTIKELWORT or w.lower() in _KEIN_ARTIKELWORT for w in woerter)


def _ist_zahlart(zeile: str) -> bool:
    """Zahlart-Zeile? Geprueft am Original UND an der entklebten Fassung.

    BEIDES ist noetig, und das war ein Beinahe-Fehler: nur entklebt zu pruefen
    zerlegte „UlSA 8.40" (OCR liest das grosse I als kleines l) zu „Ul SA" —
    ein dokumentierter Fall, der vorher sauber gefiltert wurde, waere wieder als
    Position durchgerutscht.
    """
    return bool(_SKIP_KARTE.search(zeile) or _SKIP_KARTE.search(_entklebt(zeile)))

# Anschrift und Kontakt des Ladens. Sie stehen im Kopf, tragen keinen Preis —
# und bekamen deshalb den erstbesten Betrag der Zeile zugeschlagen. Gemessen an
# einem Apotheken-Beleg: „Bahnhofplata,1234 Musterstadt Telefon0417201…" stand als
# Position da, mit dem Beleg-Total als Preis.
#
# Drei Formen, und jede einzeln bewusst schwach:
#   * Postleitzahl mit Ort — vier ALLEINSTEHENDE Ziffern und ein
#     grossgeschriebenes Wort. „Alleinstehend" heisst: weder Ziffer noch
#     BUCHSTABE daneben. Die Schranke stand zuerst nur gegen Ziffern, und die
#     Regel griff damit jede Artikelnummer, die an ihrem Namen klebt: aus
#     „Schrauben4023 Sortiment  12.50" wurde eine Anschrift, und die Position
#     fiel weg. Nachgemessen an zufaelligen Namen mit vierstelliger Nummer:
#     jeder zwanzigste Beleg verlor so eine Zeile.
#     Gegen Ziffern muss die Schranke ebenfalls stehen — sonst griff die Regel
#     die letzten vier Stellen eines EAN-Codes, und ein OBI-Bon
#     („7610000000001  Gruenduengung") verlor jede Position.
#   * Ein Strassenwort. OHNE Wortgrenze davor, weil die Strasse fast immer
#     angehängt ist („Bahnhofplatz", „Musterstrasse"), und ohne Hausnummer
#     dahinter, weil die Zerlegung sie schon als Teil des Betrags weggenommen
#     hat („Bahnhofplatz 1 172.89" → Kopf „Bahnhofplatz").
#   * Das Wort Telefon.
# Ein Artikelname trifft keine davon: Strassenwörter kommen in Waren nicht vor,
# eine vierstellige Zahl vor einem Ortsnamen ebenso wenig.
_SKIP_ANSCHRIFT = re.compile(
    r"(?<![0-9A-Za-zÄÖÜäöü])\d{4}(?![0-9A-Za-zÄÖÜäöü])\s*[A-ZÄÖÜ][a-zäöü]{2,}"
    r"|(?:strasse|str\.|weg|platz|gasse|allee)(?![a-zäöü])"
    r"|\btele(?:fon|phon)|\btel\.?\s*\d",
    re.IGNORECASE,
)

# Datums-/Zeit-/UID-Fragmente: aus jeder Zeile entfernt, BEVOR Preise gesucht werden —
# sonst landet eine Referenz-/Footer-Zeile als Position mit einem Datums-Teil („20.06" aus
# „20.06.2026") oder UID-Ziffern als Preis (realer Karma-Bug: „…HKKE300/ 20.06").
_NOISE_STRIP = re.compile(
    r"\d{1,2}[.,]\d{1,2}[.,]\d{2,4}|\b\d{1,2}:\d{2}(?::\d{2})?\b|"
    r"CHE[-\s]?\d{3}[.\s]?\d{3}[.\s]?\d{3}",
    re.IGNORECASE,
)

# Eingebautes Positions-Lexikon: Gruppenlabel -> Stichwörter. Pro Gruppe folgen
# Kandidaten-Namen, über die wir die passende echte Kategorie des Nutzers suchen.
_LEX: list[tuple[str, tuple[str, ...], tuple[str, ...], str]] = [
    # (label, item_keywords, category_name_needles, match_mode)
    ("Alkohol",
     ("wein", "bier", "prosecco", "sekt", "vodka", "wodka", "whisky", "rum", "gin",
      "aperol", "campari", "liqueur", "likör", "likoer", "schnaps", "spirituose"),
     ("alkohol", "wein", "bier", "spirituose"), "startswith"),
    ("Drogerie",
     ("shampoo", "duschgel", "seife", "zahnpasta", "zahnb", "deo", "creme", "lotion",
      "windel", "binden", "tampon", "rasier", "watte", "sonnencreme", "pflaster"),
     ("drogerie", "hygiene", "gesundheit", "körper", "koerper", "pflege"), "contains"),
    ("Haushalt",
     ("spülmittel", "spuelmittel", "putz", "reiniger", "waschmittel", "weichsp",
      "haushaltp", "küchenrolle", "kuechenrolle", "müllbeutel", "muellbeutel",
      "abfallsack", "schwamm", "alufolie", "backpapier", "batterie", "glühbirne",
      "gluehbirne", "kerze", "servietten", "wc-"),
     ("haushalt", "wohnen", "reinigung"), "contains"),
    ("Genuss",
     ("chips", "schoggi", "schokolade", "chocolat", "glace", "snack", "guetzli",
      "biscuit", "bonbon", "gummibär", "gummibaer", "cracker", "salzst", "popcorn",
      "energy", "redbull", "monster", "zigi", "zigaret", "tabak"),
     ("genuss", "süss", "suess", "snack", "naschen", "hobby", "freizeit"), "contains"),
    ("Lebensmittel",
     ("brot", "brötchen", "broetchen", "milch", "butter", "käse", "kaese", "joghurt",
      "jogurt", "eier", "banane", "apfel", "äpfel", "aepfel", "birne", "orange",
      "gemüse", "gemuese", "salat", "tomate", "gurke", "zwiebel", "kartoffel", "rüebli",
      "ruebli", "karotte", "fleisch", "poulet", "huhn", "wurst", "schinken", "speck",
      "fisch", "lachs", "thon", "pasta", "spaghetti", "reis", "mehl", "zucker", "salz",
      "kaffee", "kafi", "tee", "wasser", "saft", "müesli", "muesli", "frucht", "obst",
      "rahm", "quark", "teig", "pizza", "suppe", "sauce", "honig", "konfi", "nudel"),
     ("lebensmittel", "essen", "einkauf", "ernährung", "ernaehrung", "lebens", "food",
      "nahrung", "haushalt"), "contains"),
]


def _to_decimal(raw: str) -> Decimal | None:
    s = raw.replace("'", "").replace("’", "").replace(" ", "")
    s = s.replace(",", ".") if ("," in s and "." not in s) else s.replace(",", "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _alpha_len(s: str) -> int:
    """Anzahl reiner Buchstaben (Plausibilität eines Positionsnamens)."""
    return len(re.sub(r"[^A-Za-zÄÖÜäöü]", "", s))


# Stückzahlen, die :func:`stueckzahl_aus_zeile` liest, BEVOR _clean_name sie entfernt.
# Bewusst nur ganze Zahlen mit Stück-Einheit: „0.5 kg" ist ein Gewicht, keine Menge.
# Die Wortgrenze am Ende ist nicht kosmetisch: ohne sie liest „2 Stangen Brot"
# ein „2 St" und halbiert den Preis.
_STUECK_FUEHREND = re.compile(r"^(\d{1,2})\s*(?:[xX*]|st|stk|st(?:ü|ue)ck)\b", re.IGNORECASE)
_STUECK_NACHGESTELLT = re.compile(r"\b(\d{1,2})\s*(?:stk|st(?:ü|ue)ck)\b", re.IGNORECASE)
# Mengenspalte eines Tabellen-Belegs: die letzte Zahl vor der ersten Preisspalte
# („Zitronenwasser 50CL   2   1.60   3.20"). Allein ist sie NICHT verwertbar — sie sieht
# aus wie eine Artikelnummer; erst die Rechnung in :func:`_menge_und_total` entscheidet.
_MENGE_SPALTE = re.compile(r"(?:^|\s)(\d{1,2})\s*$")


def _clean_name(s: str) -> str:
    """Bereinigt einen Positionsnamen für die Anzeige. **Volumen-Angaben bleiben** (z. B.
    „50CL", „250G", „33CL"), aber raus fliegen: führende Artikel-/EAN-Nummer, Stück-Mengen
    (führendes „2x", Multipack „6x" VOR einem Volumen, führende „1 ST"/„1 "), nachgestellte
    Mengen-/Zahlenspalten („ I"/„ 1") und Rand-Zeichen."""
    s = _LEAD_EAN.sub("", s).strip(" .:-–—x*\t|")                # führende Artikel-/EAN-Nummer
    s = re.sub(r"^\d+\s*[xX*/]\s*", "", s)                       # führende Stück-Menge „2x "
    s = re.sub(r"\b\d+\s*[xX]\s*(?=\d)", "", s)                  # Multipack „6x" vor Volumen: „6x50CL" → „50CL"
    s = re.sub(r"^\d+[.,]?\d*\s*(st(?:k|ck)?|stück|stueck|kg|g|ml|l|x)?\b", "", s, flags=re.IGNORECASE)  # führende Menge „1 ST"/„1 "
    s = re.sub(r"(?:\s+[\d.,'’)(/xX*%+\-Il|]+)+$", "", s)        # nachgestellte Mengen-/Zahlenspalten („ I", „ 1")
    return s.strip(" .:-–—x*\t|")


def _looks_like_code(name: str) -> bool:
    """True, wenn der „Name" eher eine Referenz/Barcode/Kassennummer ist als ein
    Produktname: ein langes Token aus GROSSBUCHSTABEN/Ziffern OHNE Kleinbuchstaben (echte
    Positionsnamen tragen Kleinbuchstaben, z. B. „Draft Dose"). Hält OCR-Müll wie
    „INRRERRYRHKKE300/" aus der Positionsliste. Ein einziges wiederholtes Zeichen
    (maskierte Kartennummer „XXXXXXXXXXXX") ist immer Code — auch ohne Ziffer."""
    compact = name.replace(" ", "")
    if len(compact) >= 6 and len(set(compact.lower())) == 1:
        return True  # „XXXXXXXXXXXX" — Kartenmaske
    return (
        len(compact) >= 8
        and not re.search(r"[a-zäöü]", name)
        and bool(re.search(r"\d", compact))
    )


def stueckzahl_aus_zeile(roh: str) -> int:
    """Stückzahl einer Belegzeile — 1, wenn keine eindeutige erkennbar ist.

    Muss VOR :func:`_clean_name` laufen: das entfernt die Menge, damit der
    Anzeigename lesbar bleibt. Danach ist sie nicht mehr rekonstruierbar, der
    Preis ist aber weiterhin das Total für alle Stück.

    Erkannt werden hier nur zwei eindeutige Schreibweisen — führend („2 x Butter",
    „2 ST Butter") und nachgestellt („Gipfeli 3 Stk"). Die **Mengenspalte** von
    Tabellen-Belegen liest :func:`_menge_und_total`, und zwar nur, wenn die Zeile
    ihre eigene Rechnung bestätigt; für sich allein ist die Spalte von einer
    Artikelnummer nicht zu unterscheiden („El Tony 33CL  2 19  3.80").

    Gewichte („0.5 kg") sind keine Stückzahl und werden bewusst nicht geteilt.
    """
    m = _STUECK_FUEHREND.match(roh.strip()) or _STUECK_NACHGESTELLT.search(roh)
    if not m:
        return 1
    try:
        n = int(m.group(1))
    except (ValueError, TypeError):
        return 1
    return n if 2 <= n <= 50 else 1


def _menge_und_total(kopf: str, werte: list[Decimal]) -> tuple[int, Decimal]:
    """Stückzahl und **Zeilen-Total** einer Positionszeile.

    Das Total ist normalerweise das rechteste Geld-Token (Tabellen-Beleg: die Spalte
    ganz rechts). Trägt die Zeile eine Mengenspalte, entscheidet stattdessen die
    Rechnung: geht ``Menge × Einzelpreis`` in einem anderen Token der Zeile auf, ist
    DAS das Zeilen-Total — und die Menge ist damit bewiesen statt geraten. Genau so
    fällt auch die Artikelnummer durch, die neben der Mengenspalte steht: für sie
    geht die Rechnung nicht auf, und es bleibt beim rechtesten Token.
    """
    total = werte[-1]
    menge = stueckzahl_aus_zeile(kopf)  # eindeutige Schreibweisen („2 x", „6 ST")
    spalte = _MENGE_SPALTE.search(kopf)
    gemessen = menge if menge >= 2 else (int(spalte.group(1)) if spalte else 1)
    if 2 <= gemessen <= 50:
        stimmig = [u * gemessen for u in werte if u * gemessen in werte]
        if stimmig:
            return gemessen, max(stimmig)
    return menge, total


def parse_receipt_items(text: str | None) -> list[tuple[str, Decimal]]:
    """Erkennt (Positionsname, Preis) aus einem Belegtext — siehe
    :func:`parse_receipt_items_menge`, hier ohne die Stückzahl."""
    return [(name, preis) for name, preis, _ in parse_receipt_items_menge(text)]


def parse_receipt_items_menge(text: str | None) -> list[tuple[str, Decimal, int]]:
    """Erkennt (Positionsname, Preis, Stückzahl) aus einem Belegtext. Best-effort.

    Der Preis ist das Positions-**Total**, die Stückzahl steht daneben — wer den
    Stückpreis braucht (Preisverlauf), teilt selbst. Zusammenfassen liesse sich
    beides nicht: für die Beleg-Aufteilung zählt das Total, für den Preisvergleich
    der Stückpreis.

    Robust gegen drei häufige Fallen: (1) Beleg-Kopf/-Fuss-Metadaten (Öffnungszeit,
    Bon-ID, Adresse, Karten-/Zahldetails) werden NICHT als Position gelesen; (2) bei
    mehrzeiligen Positionen (Artikelname in einer oder mehreren Zeilen, „1 ST … Preis"
    in der nächsten — z. B. Baumarkt-/OBI- und Gastro-Belege) werden ALLE Namenszeilen
    davor zusammengeführt, nicht nur die letzte; (3) bei Tabellen-Belegen (Migros:
    Menge|Preis|Aktion|Total mit OCR-Müll am Zeilenende) wird das rechteste Geld-Token
    der Zeile als Positions-Total genommen — ausser die Mengenspalte weist ein anderes
    Token als Zeilen-Total aus (:func:`_menge_und_total`).
    """
    items: list[tuple[str, Decimal, int]] = []
    # Namenszeilen der laufenden Position. Mehrere, weil ein Name über zwei bis drei
    # Zeilen laufen kann; nur die letzte zu behalten zerschnitt ihn („Jalapenos" statt
    # „Grosse Nachoschale mit Käsedip und Jalapenos").
    pending: list[str] = []
    for raw in (text or "").splitlines():
        line = _NOISE_STRIP.sub(" ", raw.strip())  # Datums-/Zeit-/UID-Fragmente raus
        if len(line) < 3:
            continue
        # Alle Geldbeträge der Zeile; das ERSTE markiert das Ende des Namens-Kopfs.
        prices = list(_PRICE_TOKEN.finditer(line))
        kopf = line[: prices[0].start()] if prices else line
        # Jeder Waechter wird auch an der ENTKLEBTEN Fassung gefragt, wo das
        # gefahrlos ist: OCR zieht Woerter zusammen, und dann greift keine
        # Wortgrenze mehr. Bei `_SKIP` ist es NICHT gefahrlos (siehe
        # `_nur_bonwoerter`), dort steht die strengere Bedingung.
        if (_SKIP.search(line) or _SKIP_GLUED.search(line) or _ist_zahlart(line)
                or _nur_bonwoerter(kopf)
                or _SKIP_ANSCHRIFT.search(kopf) or _SKIP_ANSCHRIFT.search(_entklebt(kopf))
                or _HAS_TIME.search(line)
                or _EINHEIT_KOPF.search(kopf) or _EINHEIT_KOPF.search(_entklebt(kopf))):
            pending.clear()  # Footer/Zahlart/Zeit erreicht → alte Namenszeilen verwerfen
            continue
        if prices:
            werte = [w for w in (_to_decimal(m.group(0)) for m in prices) if w is not None]
            menge, price = _menge_und_total(kopf, werte) if werte else (1, None)
            if price is not None and Decimal("0") < price <= Decimal("10000"):
                inline = _clean_name(kopf)
                # Inline-Name nehmen, wenn er echte Buchstaben trägt; sonst den Namen
                # aus den Vorzeilen (mehrzeilige Position, z. B. Baumarkt/OBI).
                mehrzeilig = " ".join(pending)
                name = inline if _alpha_len(inline) >= 3 else (
                    mehrzeilig if _alpha_len(mehrzeilig) >= 3 else inline
                )
                if _alpha_len(name) >= 3 and not _looks_like_code(name):
                    items.append((name, price, menge))
                pending.clear()
                continue
        # Keine (gültige) Preiszeile → ggf. als Namenszeile für die nächste Position.
        if not _NUM_LINE.match(line):
            cand = _clean_name(line)
            if _alpha_len(cand) >= 3:
                pending.append(cand)
                del pending[:-_NAME_ZEILEN]
    return items


def _guess_label(name: str, pairs: list[tuple[str, int]]) -> tuple[str, str | None]:
    """Schätzt für einen Positionsnamen das Lexikon-Label + ggf. Regel-Kategorie.

    Liefert (label, None) bei Lexikon-Treffer; ("__rule__", "<rule-cat>") gibt es
    nicht — Regeln liefern direkt eine category_id, daher separat behandelt.
    """
    low = name.lower()
    tokens = re.findall(r"[a-zäöü]+", low)
    for label, item_kw, _needles, mode in _LEX:
        for kw in item_kw:
            if mode == "startswith":
                if any(tok.startswith(kw) for tok in tokens):
                    return label, None
            else:
                if kw in low:
                    return label, None
    return "", None


def _resolve_label_category(label: str, cats: list[Category]) -> int | None:
    """Sucht zu einem Lexikon-Label die passende echte Kategorie des Nutzers."""
    for lex_label, _kw, needles, _mode in _LEX:
        if lex_label != label:
            continue
        for needle in needles:
            for c in cats:
                if needle in (c.name or "").lower():
                    return c.id
    return None


def suggest_splits(db: Session, tx: Transaction, ocr_text: str | None) -> dict:
    """Baut einen Aufteilungs-Vorschlag für ``tx`` aus dem Belegtext.

    Rückgabe: ``{"rows": [...], "note": str, "ok": bool}``. Jede Zeile ist ein
    Dict ``{"category_id": int|"", "amount": Decimal(positiv)}``. Die Beträge
    werden — wenn die Positions-Summe nah genug am Buchungsbetrag liegt — exakt
    auf den Buchungsbetrag angepasst, damit der Nutzer direkt speichern kann.
    """
    target = (tx.amount or Decimal("0")).copy_abs()
    items = parse_receipt_items(ocr_text)
    if not items:
        return {
            "rows": [],
            "note": "Keine Positionen aus dem Beleg erkennbar — bitte manuell aufteilen.",
            "ok": False,
        }

    pairs = load_active_rules(db)
    cats = list(db.scalars(select(Category)))
    label_cache: dict[str, int | None] = {}
    fallback = tx.category_id  # bisherige Kategorie der Buchung (kann None sein)

    groups: dict[object, Decimal] = {}
    for name, price in items:
        label, _ = _guess_label(name, pairs)
        cid: int | None = None
        if label:
            if label not in label_cache:
                label_cache[label] = _resolve_label_category(label, cats)
            cid = label_cache[label]
        if cid is None:
            cid = match_category(pairs, name)  # Händler-Regel als Zweitchance
        if cid is None:
            cid = fallback
        key = cid if cid is not None else "__none__"
        groups[key] = groups.get(key, Decimal("0")) + price

    item_sum = sum(groups.values(), Decimal("0"))
    ordered = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
    rows = [
        {"category_id": (k if k != "__none__" else ""), "amount": v.quantize(Decimal("0.01"))}
        for k, v in ordered
    ]

    diff = target - item_sum
    tol = max(Decimal("2.00"), (target * Decimal("0.20")).quantize(Decimal("0.01")))
    close = target > 0 and abs(diff) <= tol
    if close and rows and diff != 0:
        # Differenz (Rundung/übersehene Kleinposition) auf die grösste Gruppe legen.
        rows[0]["amount"] = (rows[0]["amount"] + diff).quantize(Decimal("0.01"))
        if rows[0]["amount"] <= 0:
            rows[0]["amount"] = Decimal("0.01")

    n_items = len(items)
    n_cats = len({r["category_id"] for r in rows})
    if close:
        note = (
            f"{n_items} Positionen erkannt, zu {n_cats} Kategorie(n) zusammengefasst "
            f"(an Buchungsbetrag angepasst). Bitte prüfen und speichern."
        )
        ok = True
    else:
        note = (
            f"{n_items} Positionen erkannt, aber ihre Summe (CHF {item_sum:.2f}) weicht "
            f"vom Buchungsbetrag (CHF {target:.2f}) ab — bitte Beträge prüfen."
        )
        ok = False
    return {"rows": rows, "note": note, "ok": ok}
