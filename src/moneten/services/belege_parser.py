"""Werte aus Beleg-Texten lesen: Prämie, Strom, Vorsorge, Police, Steuern.

Reine Textfunktionen — **kein Dateizugriff, kein PDF**. Das Auslesen der
Textebene macht das lokale Skript ``scripts/verlaeufe_aus_scans.py``; hier
steht nur die Deutung. Dadurch ist jede Regel mit erfundenem Text testbar,
ohne dass je ein echter Beleg in die Tests gerät.

**Nicht jeder Beleg ist gleich verlässlich.** Prämienabrechnung, Police,
Stromrechnung und Vorsorgeausweis tragen native Textebenen — was dort steht,
stand so im PDF. Prämienverbilligung und Steuerrechnung sind dagegen
OCR-Ergebnisse; dort finden sich Lesefehler bis in einzelne Buchstaben
(„Ordentiiche" statt „Ordentliche", verdrehte Buchstaben in Namen). Befunde aus solchen Quellen bekommen
``unsicher=True`` und werden beim Import zur Bestätigung vorgelegt, statt still
übernommen zu werden. Eine falsche Zahl, die unbemerkt in einem Verlauf landet,
ist schlimmer als eine, die nachfragt.

**Zwei Textformen.** Die meisten Parser bekommen die Lesereihenfolge des PDFs,
so wie ``page.get_text()`` sie liefert. :func:`rechnung_nach_profil` bekommt
stattdessen **Spaltentext**: eine Zeile je Tabellenzeile, die Zellen durch
Tabulator getrennt (siehe ``pdf_spalten`` im Extraktionsskript). Der Grund steht
bei der Funktion — in der Lesereihenfolge ist dort nicht zu erkennen, welche
Zahl der Betrag ist. Beide Formen sind reiner Text und damit mit erfundenen
Beispielen prüfbar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial

from moneten.config import settings
from moneten.services.anbieter_profil import Anbieterprofil, lade_profile

# Zweistellige Jahre kommen auf der Stromrechnung vor („01.04.26 - 30.06.26").
# Alles unter dieser Schwelle ist 20xx. Die Belege beginnen 2016; ein Wert wie
# „70" käme nur aus einem Lesefehler und soll dann auch auffällig alt wirken.
_JAHRHUNDERT_GRENZE = 70


# Untergrenzen je Reihe. Kein Feintuning, sondern ein Filter gegen offensichtlich
# falsch gegriffene Zahlen: Ein Lesefehler trifft meist eine Referenznummer, eine
# Seitenzahl oder eine Teilposition — die liegen um Grössenordnungen daneben.
# Gemessen an den echten Belegen lieferte die Steuer- und Verbilligungserkennung
# ohne diese Grenze Werte wie „CHF 4" Prämienverbilligung und „0.00" Altersguthaben.
_MINDESTWERT: dict[str, Decimal] = {
    "kk_praemie": Decimal("50"),
    "kk_police": Decimal("50"),
    "kk_verbilligung": Decimal("200"),
    "strom": Decimal("20"),
    "lohn": Decimal("5000"),
    "pk_guthaben": Decimal("1"),
    "steuern_kanton": Decimal("100"),
    "steuern_bund": Decimal("20"),
    "miete": Decimal("300"),
    "gesundheit_selbst": Decimal("1"),
    "nebenverdienst": Decimal("50"),
}
# Anbieter-Reihen tragen ihre Untergrenze im Profil (``toleranz``) und werden
# weiter unten nachgetragen — hier steht kein Anbietername.


def unplausibel_warum(slug: str, wert: Decimal) -> str:
    """Ein Satz, warum dieser Wert nicht durchgeht — für die Ausgabe des Skripts.

    Vorher stand dort nur „verworfen". Das las sich wie ein Fehler des Belegs,
    war aber eine Aussage über die GRÖSSENORDNUNG: eine Kantonssteuer von 89.35
    ist keine Jahresrechnung, sondern eine Zahl, die der Parser an der falschen
    Stelle gegriffen hat. Wer das nicht weiss, sucht den Fehler im Scan.
    """
    grenze = _MINDESTWERT.get(slug, Decimal("0"))
    if wert == 0:
        return ("null, und der Beleg bestätigt sie nicht ausdrücklich "
                "(eine gelesene Null kann von einer anderen Zeile stammen)")
    if grenze and wert < grenze:
        return (f"unter der Mindestgrösse dieser Reihe ({grenze}) — "
                "vermutlich an der falschen Stelle gegriffen")
    return "ausserhalb der erwarteten Grössenordnung"


def plausibel(slug: str, wert: Decimal, *, null_bestaetigt: bool = False) -> bool:
    """Liegt der Wert in einer Grössenordnung, die für diese Reihe möglich ist?

    ``null_bestaetigt`` ist die einzige Ausnahme von der Untergrenze — und sie
    gilt **nur für exakt 0**. Sie ist kein Schalter zum Abstellen des Schutzes,
    sondern eine Aussage über die Herkunft: „Wer diesen Wert gelesen hat, hat
    gesehen, dass der Beleg für DIESE Reihe null ausweist." Eine
    Steuerveranlagung mit steuerbarem Einkommen 0 nennt genau das, und ohne
    diesen Weg fehlte das Jahr im Verlauf, obwohl es belegt ist.

    Warum die Ausnahme nicht einfach für jede Null gilt: Die Untergrenze schützt
    gegen falsch gegriffene Zahlen, und eine falsch gegriffene Zahl kann sehr
    wohl 0.00 sein — auf jeder Veranlagung stehen mehrere Nullen (Vermögen,
    Personalsteuer, eine andere Steuerart). Würde 0 pauschal durchgelassen,
    landete die Null der Bundessteuer als Kantonssteuer im Verlauf. Genau davor
    schützt die Bindung an die Herkunft: Wer nur Text greift, kann sie nicht
    setzen.
    """
    grenze = _MINDESTWERT.get(slug, Decimal("0"))
    if wert == 0:
        # Ohne eigene Untergrenze hat diese Funktion keine Meinung — dann bleibt
        # die Null so zulässig, wie sie es vorher war.
        return null_bestaetigt or grenze == 0
    return wert >= grenze


@dataclass(frozen=True)
class Befund:
    """Ein gelesener Wert samt Periode und Herkunft.

    ``extras`` hält benannte Nebenwerte (kWh, Franchise, Beitrag) — sie landen
    unverändert in ``MetricPoint.extras``.
    """

    slug: str
    period_start: date
    period_end: date
    value: Decimal
    extras: dict[str, str] = field(default_factory=dict)
    unsicher: bool = False
    hinweis: str = ""
    # Der Beleg weist für DIESE Reihe ausdrücklich 0.00 aus — nur damit kommt
    # eine echte Null an der Untergrenze vorbei (siehe ``plausibel``). Setzen darf
    # das nur, wer die Zuordnung gesehen hat; aus blossem Text ist sie nicht
    # zu erkennen, weil auf jedem Beleg mehrere Nullen stehen.
    null_bestaetigt: bool = False
    # Mehrere Belege beschreiben dieselbe Periode und müssen SUMMIERT werden,
    # statt einander zu verdrängen: eine Leistungsabrechnung deckt eine einzelne
    # Behandlung ab, die Jahressumme entsteht erst aus allen zusammen. Ohne diese
    # Unterscheidung stünde im Verlauf der Betrag einer beliebigen Arztrechnung
    # statt dessen, was das Jahr gekostet hat.
    additiv: bool = False


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------

# Die Textebene bricht Wörter an Zeilenenden um („Prämi\nenverbilligung") und
# streut Mehrfach-Leerzeichen ein. Vor jedem Suchen wird darum plattgemacht.
_MEHRFACH = re.compile(r"\s+")


def flach(text: str) -> str:
    """Alle Zeilenumbrüche und Mehrfach-Leerzeichen zu einem Leerzeichen."""
    return _MEHRFACH.sub(" ", text).strip()


# Schweizer Tausendertrennung: Hochkomma. Der Lohnausweis verwendet stattdessen
# ein Akut-Zeichen (´), manche Scans einen Apostroph (’) — alle drei meinen
# dasselbe und müssen weg, bevor Decimal den Rest sieht.
_TRENNER = str.maketrans("", "", "'´’ ")


def betrag(roh: str) -> Decimal | None:
    """Schweizer Betragsschreibweise zu ``Decimal``.

    Gibt ``None`` statt zu werfen: Belegtexte enthalten Zahlen, die keine
    Beträge sind (Referenznummern, Seitenzahlen). Der Aufrufer entscheidet, ob
    ein Fehlschlag ein Problem ist.
    """
    s = roh.strip().translate(_TRENNER).rstrip(".-")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_DATUM = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")


def datum(roh: str) -> date | None:
    """``31.12.2025`` oder ``30.06.26`` zu ``date``."""
    m = _DATUM.fullmatch(roh.strip())
    if not m:
        return None
    tag, monat, jahr = (int(g) for g in m.groups())
    if jahr < 100:
        jahr += 2000 if jahr < _JAHRHUNDERT_GRENZE else 1900
    try:
        return date(jahr, monat, tag)
    except ValueError:  # 31.02. aus einem Lesefehler
        return None


def monate_zwischen(von: date, bis: date) -> list[date]:
    """Erste Tage aller Monate, die die Periode berührt — einschliesslich beider Ränder."""
    monate: list[date] = []
    lauf = von.replace(day=1)
    while lauf <= bis:
        monate.append(lauf)
        lauf = date(lauf.year + 1, 1, 1) if lauf.month == 12 else date(lauf.year, lauf.month + 1, 1)
    return monate


# ---------------------------------------------------------------------------
# Krankenkassenprämie
# ---------------------------------------------------------------------------

# Die verbreiteten Abrechnungs-Layouts der Krankenkassen (älter und neuer)
# stellen die Periode gleich dar und lassen darauf die Monatsprämie folgen. Der Zeilenaufbau unterscheidet sich,
# die Reihenfolge Periode → Prämie nicht.
_PERIODE_PRAEMIE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})\s+([\d'’´.]+)"
)


def praemienabrechnung(text: str) -> list[Befund]:
    """Monatsprämien aus einer Prämienabrechnung.

    Deckt eine Abrechnung mehrere Monate ab (Nachbelastungen wie „Jan–Mai"),
    entsteht **je Monat ein Befund** mit derselben Monatsprämie. Sonst stünde
    für vier der fünf Monate nichts da, obwohl der Beleg sie ausweist.

    Die Gesamtsumme wird bewusst nicht gelesen: sie ist Prämie × Monate und
    damit redundant — eine zweite Zahl, die bei jedem Layoutwechsel neu
    verrutschen könnte.
    """
    treffer = _PERIODE_PRAEMIE.search(flach(text))
    if not treffer:
        return []
    von, bis, roh = treffer.groups()
    d_von, d_bis, praemie = datum(von), datum(bis), betrag(roh)
    if d_von is None or d_bis is None or praemie is None or praemie <= 0:
        return []
    if d_bis < d_von:
        return []
    return [
        Befund(
            slug="kk_praemie",
            period_start=monat,
            period_end=_letzter_tag(monat),
            value=praemie,
        )
        for monat in monate_zwischen(d_von, d_bis)
    ]


def _letzter_tag(monatsanfang: date) -> date:
    """Letzter Tag des Monats zu einem 1.-des-Monats-Datum."""
    if monatsanfang.month == 12:
        return date(monatsanfang.year, 12, 31)
    naechster = date(monatsanfang.year, monatsanfang.month + 1, 1)
    return date.fromordinal(naechster.toordinal() - 1)


# ---------------------------------------------------------------------------
# Stromrechnung
# ---------------------------------------------------------------------------

_MONATE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

# Der Kopf der Stromrechnung nennt die Periode im Klartext. Bewusst dieser
# Anker und nicht „Abrechnungsperiode": das ist dort nur eine Spaltenüberschrift,
# die Daten stehen eine halbe Tabelle weiter — ein Muster, das den Zeitraum
# genau deshalb nie fand.
_STROM_PERIODE = re.compile(
    r"Zeitraum vom\s+(\d{1,2})\.\s*([A-Za-zäÄ]+)\s+(\d{4})"
    r"\s+bis\s+(\d{1,2})\.\s*([A-Za-zäÄ]+)\s+(\d{4})",
    re.IGNORECASE,
)
_STROM_BETRAG = re.compile(r"Rechnungsbetrag\s+([\d'’.]+)")
# Viele Werke rechnen mit Akonto ab: Quartalsrechnungen sind Vorauszahlungen,
# die echten Kosten stehen erst in der Jahresrechnung.
_STROM_ART = re.compile(r"\b(Akontorechnung|Jahresrechnung|Quartalsrechnung)\b")
# Auf der Jahresrechnung ist der „Rechnungsbetrag" nur die Restzahlung nach Abzug
# der Akontos. Die tatsächlichen Jahreskosten stehen davor — die Zahl unmittelbar
# vor der ersten Abzugszeile.
_STROM_VOR_AKONTO = re.compile(r"([\d'’.]+)\s+Abzüglich Akontozahlung")
# Wirkenergie steht zweimal (Energie- und Netzanteil) mit identischer Menge.
_STROM_KWH = re.compile(r"Wirkenergie\s+\d{2}\.\d{2}\.\d{2}\s*-\s*\d{2}\.\d{2}\.\d{2}"
                        r"\s*\(\d+\s*Tage\)\s+([\d'’]+)\s+kWh")


def _klartext_datum(tag: str, monat: str, jahr: str) -> date | None:
    """``1`` / ``Oktober`` / ``2024`` zu ``date``."""
    nr = _MONATE.get(monat.strip().lower())
    if nr is None:
        return None
    try:
        return date(int(jahr), nr, int(tag))
    except ValueError:
        return None


def stromrechnung(text: str) -> list[Befund]:
    """Stromkosten und Verbrauch einer Stromrechnung.

    **Akontorechnungen liefern keinen Befund.** Sie sind Vorauszahlungen, keine
    Kosten: bei Akonto-Abrechnung stellt das Werk je Quartal einen
    Pauschalbetrag und rechnet erst am Jahresende ab. Würden sie als Kosten mitlaufen, stünden für
    dasselbe Jahr die Vorauszahlungen *und* die Abrechnung in der Kurve — ein
    Verlauf, den es nie gab.

    Auf der Jahresrechnung ist der ausgewiesene „Rechnungsbetrag" nur die
    Restzahlung nach Abzug der Akontos. Als Kosten zählt der Betrag davor.

    Der Verbrauch ist der eigentliche Gewinn dieser Serie: erst mit den kWh
    daneben ist zu erkennen, ob eine höhere Rechnung von mehr Verbrauch oder
    von einem höheren Tarif kommt. Fehlt er, entsteht der Befund trotzdem —
    ein Betrag ohne Verbrauch ist besser als gar nichts.
    """
    f = flach(text)
    art_treffer = _STROM_ART.search(f)
    art = art_treffer.group(1) if art_treffer else "Quartalsrechnung"
    if art == "Akontorechnung":
        return []
    p = _STROM_PERIODE.search(f)
    if not p:
        return []
    b = (
        _STROM_VOR_AKONTO.search(f) if art == "Jahresrechnung" else _STROM_BETRAG.search(f)
    )
    if not b:
        return []
    d_von = _klartext_datum(*p.group(1, 2, 3))
    d_bis = _klartext_datum(*p.group(4, 5, 6))
    chf = betrag(b.group(1))
    if d_von is None or d_bis is None or chf is None or d_bis < d_von:
        return []
    extras: dict[str, str] = {"rechnungsart": art}
    # Auf der Jahresrechnung stehen mehrere Wirkenergie-Zeilen (je Teilperiode und
    # Tarif). Die erste davon deckt nur ein Quartal ab — mit dem Jahresbetrag
    # gepaart ergäbe das einen dreifach zu hohen Rappenpreis. Lieber kein
    # Verbrauch als ein falscher: die Quartalsrechnungen liefern ihn ohnehin.
    k = None if art == "Jahresrechnung" else _STROM_KWH.search(f)
    if k and (kwh := betrag(k.group(1))) is not None and kwh > 0:
        extras["kwh"] = str(kwh)
        # Rappen je kWh — die Grösse, die Tarifänderungen sichtbar macht.
        # Aus Betrag und Menge gerechnet, nicht gelesen: die Rechnung führt
        # mehrere Teiltarife (Energie, Netz, Abgaben), deren Summe genau das ist.
        extras["rp_kwh"] = str((chf / kwh * 100).quantize(Decimal("0.01")))
    return [Befund(slug="strom", period_start=d_von, period_end=d_bis, value=chf, extras=extras)]


# ---------------------------------------------------------------------------
# Vorsorgeausweis (Pensionskasse) — liefert zugleich den Lohn
# ---------------------------------------------------------------------------

_VA_STICHTAG = re.compile(r"Vorsorgeausweis\s+per\s+(\d{2}\.\d{2}\.\d{4})")
_VA_AHV_LOHN = re.compile(r"AHV-Jahreslohn\s+([\d'’.]+)")
_VA_VERS_LOHN = re.compile(r"Versicherter Jahreslohn\s*\d*\s+([\d'’.]+)")
_VA_PENSUM = re.compile(r"Beschäftigungsgrad\s+([\d.]+)\s*%")
# Zeile „Total Monatsbeitrag  <AN>  <AG>  <Total>".
_VA_BEITRAG = re.compile(r"Total Monatsbeitrag\s+([\d'’.]+)\s+([\d'’.]+)\s+([\d'’.]+)")
# Zeile „Altersguthaben   <BVG-Anteil>  <Total>" — die zweite Zahl
# ist das gesamte Guthaben. Es gibt eine gleichnamige Zeile für den Jahresanfang;
# gesucht ist die mit dem Stichtag des Ausweises.
_VA_GUTHABEN = re.compile(r"Altersguthaben am (\d{2}\.\d{2}\.\d{4})\s+([\d'’.]+)\s+([\d'’.]+)")


def vorsorgeausweis(text: str) -> list[Befund]:
    """Altersguthaben und Jahreslohn aus einem Vorsorgeausweis.

    Zwei Reihen aus einem Beleg: das Guthaben ist ein **Bestand** zum Stichtag,
    der Lohn eine **Jahresgrösse**. Sie zusammen in eine Reihe zu werfen wäre
    bequem und falsch — Beträge in Franken heisst nicht dasselbe gemessen.
    """
    f = flach(text)
    s = _VA_STICHTAG.search(f)
    if not s or (stichtag := datum(s.group(1))) is None:
        return []

    befunde: list[Befund] = []

    # Guthaben: der Eintrag zum Stichtag des Ausweises, nicht der zum Jahresanfang.
    for m in _VA_GUTHABEN.finditer(f):
        if datum(m.group(1)) != stichtag:
            continue
        if (total := betrag(m.group(3))) is None:
            continue
        extras: dict[str, str] = {}
        if (bt := _VA_BEITRAG.search(f)) is not None:
            for schluessel, gruppe in (("beitrag_an", 1), ("beitrag_ag", 2),
                                       ("beitrag_monat", 3)):
                if (wert := betrag(bt.group(gruppe))) is not None:
                    extras[schluessel] = str(wert)
        befunde.append(Befund(
            slug="pk_guthaben", period_start=stichtag, period_end=stichtag,
            value=total, extras=extras,
        ))
        break

    # Lohn: gilt für das Kalenderjahr des Stichtags.
    if (lohn := _VA_AHV_LOHN.search(f)) is not None and (jahres := betrag(lohn.group(1))):
        extras = {}
        if (vl := _VA_VERS_LOHN.search(f)) is not None and (v := betrag(vl.group(1))):
            extras["versicherter_lohn"] = str(v)
        if (pg := _VA_PENSUM.search(f)) is not None and (p := betrag(pg.group(1))):
            extras["pensum"] = str(p)
        befunde.append(Befund(
            slug="lohn",
            period_start=date(stichtag.year, 1, 1),
            period_end=date(stichtag.year, 12, 31),
            value=jahres,
            extras=extras,
            # Der Ausweis nennt den Lohn zum Stichtag hochgerechnet aufs Jahr.
            # Wechselt die Stelle oder das Pensum mitten im Jahr, weicht das vom
            # tatsächlich ausbezahlten Jahreslohn ab.
            hinweis="Hochgerechnet aus dem Vorsorgeausweis, nicht der ausbezahlte Jahreslohn.",
        ))
    return befunde


# ---------------------------------------------------------------------------
# Krankenkassen-Police
# ---------------------------------------------------------------------------

# Der Doppelpunkt ist optional: ältere Policen schreiben „Gültig ab: 01.01.2021",
# neuere „GÜLTIG AB 01.01.2025". Ohne ihn fand das Muster nur die neuen.
_POLICE_AB = re.compile(r"Gültig ab:?\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
# Zwischen Bezeichnung und Prämie steht der Modellname (je nach Kasse etwa
# „MODELL BASIS"). Er wird mitgelesen: ein Modellwechsel erklärt Sprünge in
# der Kurve, die sonst wie eine Preiserhöhung aussähen.
_POLICE_KVG = re.compile(
    r"Obligatorische Krankenpflegeversicherung\s*([A-ZÄÖÜ][A-ZÄÖÜ0-9 ]{2,40}?)?\s*([\d'’.]+)",
    re.IGNORECASE,
)
_POLICE_FRANCHISE = re.compile(r"Jahresfranchise beträgt CHF\s+([\d'’.]+)", re.IGNORECASE)


def police(text: str) -> list[Befund]:
    """Sollprämie und Franchise aus einer Versicherungspolice.

    Der Wert ist die **Grundversicherung (KVG)**, nicht die Gesamtprämie: nur
    sie ist über die Jahre vergleichbar. Zusatzversicherungen kommen und gehen,
    ihre Summe würde einen Wechsel wie eine Preiserhöhung aussehen lassen.
    """
    f = flach(text)
    ab = _POLICE_AB.search(f)
    kvg = _POLICE_KVG.search(f)
    if not ab or not kvg:
        return []
    start = datum(ab.group(1))
    monatspraemie = betrag(kvg.group(2))
    if start is None or monatspraemie is None or monatspraemie <= 0:
        return []
    extras: dict[str, str] = {}
    if (modell := kvg.group(1)) and (sauber := modell.strip()):
        extras["modell"] = sauber
    if (fr := _POLICE_FRANCHISE.search(f)) is not None and (w := betrag(fr.group(1))):
        extras["franchise"] = str(w)
    return [Befund(
        slug="kk_police",
        period_start=date(start.year, 1, 1),
        period_end=date(start.year, 12, 31),
        value=monatspraemie,
        extras=extras,
    )]


# ---------------------------------------------------------------------------
# Hausrat- und Privathaftpflicht-Police (Sachversicherung, OCR-Text)
# ---------------------------------------------------------------------------
#
# NICHT zu verwechseln mit :func:`police` — die liest die KRANKENkassen-Police
# und sucht dort die KVG-Zeile. Genau daran ging diese Police vorbei: ihr
# Dateiname enthaelt „Versicherungspolice", die Zuordnung schickte sie an den
# KVG-Parser, der keine KVG-Zeile fand und stillschweigend nichts zurueckgab.
# Die Reihe „Hausrat / Privathaftpflicht" blieb darum leer, obwohl der Beleg
# seit Juli 2024 im Ordner liegt.
#
# Die Police hat eine EIGENE Pruefsumme, und die wird hier auch benutzt:
#   Hausrat + Privathaftpflicht = Versicherungspraemie
#   Versicherungspraemie + Stempelabgabe = Jahrespraemie
# Geht sie nicht auf, ist der Beleg falsch gelesen — dann lieber ein Fehler als
# eine Zahl, die niemand nachrechnet.

# OCR verliert die Umlaute dieser Police zuverlaessig: gemessen kamen
# „Pramienubersicht", „Jahrespramie" und „Falligkeit" vor. Auf den Umlauten zu
# bestehen kostete den ganzen Beleg, darum steht ueberall [aä] bzw. [uü].
# Ein Betrag endet nach den Rappen. Die erste Fassung stand auf
# ``[\d'’.,\s]+`` und durfte damit ueber ein Leerzeichen hinweg weiterlesen —
# gemessen an der Police von 2023 wurde aus „61.80" die Zahl „61.8030183",
# weil die naechste Spalte mitgelesen wurde. Der Wert stimmte trotzdem, weil
# die Summe aufging; in der Aufstellung stand danach eine Zahl mit sieben
# Nachkommastellen.
_HR_BETRAG = r"(\d[\d'’\s]{0,12}[.,]\d{2})(?!\d)"

_HR_BEGINN = re.compile(r"Vertragsbeginn:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.IGNORECASE)
_HR_JAHR = re.compile(r"Jahrespr[aä]mie\s*CHF\s*" + _HR_BETRAG, re.IGNORECASE)
_HR_SUMME = re.compile(r"Versicherungspr[aä]mie\s*CHF\s*" + _HR_BETRAG, re.IGNORECASE)
_HR_STEMPEL = re.compile(r"Stempelabgabe\s*CHF\s*" + _HR_BETRAG, re.IGNORECASE)
_HR_HAUSRAT = re.compile(r"Hausratversicherung[^C]{0,60}CHF\s*" + _HR_BETRAG, re.IGNORECASE)
_HR_HAFTPFLICHT = re.compile(
    r"Privathaftpflichtversicherung[^C]{0,40}CHF\s*" + _HR_BETRAG, re.IGNORECASE
)

# Rundungsspielraum der Pruefsumme. Die Police rechnet in Rappen; fuenf Rappen
# fangen ab, dass OCR eine Ziffer am Rand verliest, ohne einen echten Lesefehler
# durchzulassen.
HAUSRAT_TOLERANZ = Decimal("0.05")


def hausrat_police(text: str) -> list[Befund]:
    """Jahrespraemie der Hausrat-/Privathaftpflicht-Police.

    Der Wert ist die **Jahrespraemie inklusive Stempelabgabe** — das ist der
    Betrag, der vom Konto geht, und damit der einzige, der sich gegen die
    Buchung abgleichen laesst.

    Die Aufteilung reist als Positionen mit (``pos:``), wie bei einer
    Anbieter-Rechnung: Hausrat, Privathaftpflicht und Stempelabgabe. Erst
    dadurch ist eine steigende Praemie als „mehr Deckung" oder „teurer geworden"
    lesbar.

    Die Periode ist das KALENDERJAHR des Vertragsbeginns, nicht das
    Versicherungsjahr ab Beginndatum. Die Reihe ist jaehrlich, und ein Punkt,
    der vom 16.07. bis zum 15.07. laeuft, liesse sich mit keinem anderen
    Jahreswert vergleichen.

    :raises PruefsummeFehler: wenn die Teilpraemien nicht die Jahrespraemie
        ergeben.
    """
    f = flach(text)
    beginn = _HR_BEGINN.search(f)
    jahr = _HR_JAHR.search(f)
    if not beginn or not jahr:
        return []
    start = datum(beginn.group(1))
    gesamt = betrag(jahr.group(1))
    if start is None or gesamt is None or gesamt <= 0:
        return []

    teile: dict[str, Decimal] = {}
    for name, muster in (("Hausrat", _HR_HAUSRAT),
                         ("Privathaftpflicht", _HR_HAFTPFLICHT),
                         ("Stempelabgabe", _HR_STEMPEL)):
        if (treffer := muster.search(f)) is not None and (w := betrag(treffer.group(1))):
            teile[name] = w

    # Erst pruefen, dann behaupten. Die Police nennt die Zwischensumme selbst —
    # wo sie steht, wird gegen sie geprueft; sonst gegen die Jahrespraemie.
    summe = sum(teile.values(), Decimal("0"))
    # Ab ZWEI Posten, nicht erst ab dreien. Eine reine
    # Privathaftpflicht-Police traegt nur zwei Zeilen (Deckung und
    # Stempelabgabe) — und genau die lief vorher ungeprueft durch.
    if len(teile) >= 2 and abs(summe - gesamt) > HAUSRAT_TOLERANZ:
        raise PruefsummeFehler(
            f"Hausrat-Police: Positionen ergeben {summe}, ausgewiesen ist {gesamt}."
        )
    zwischen = _HR_SUMME.search(f)
    if zwischen is not None and (z := betrag(zwischen.group(1))) is not None:
        ohne_stempel = summe - teile.get("Stempelabgabe", Decimal("0"))
        if teile and abs(ohne_stempel - z) > HAUSRAT_TOLERANZ:
            raise PruefsummeFehler(
                f"Hausrat-Police: Deckungen ergeben {ohne_stempel}, "
                f"ausgewiesene Versicherungspraemie ist {z}."
            )

    extras = {POS_PRAEFIX + name: str(wert) for name, wert in teile.items()}
    return [Befund(
        slug="hausrat_haftpflicht",
        period_start=date(start.year, 1, 1),
        period_end=date(start.year, 12, 31),
        value=gesamt,
        extras=extras,
        hinweis="Jahrespraemie inklusive Stempelabgabe.",
    )]


# ---------------------------------------------------------------------------
# Prämienverbilligung (OCR-Text)
# ---------------------------------------------------------------------------

# OCR verstuemmelt genau dieses Wort zuverlaessig: gemessen kamen
# „Verfuegung", „Verfiigung" und „Verfugung" vor — ue/ii/u statt ü. Der
# Wortstamm und die Jahreszahl reichen zur Erkennung; auf die Umlaute zu
# bestehen kostete Werte, die sauber im Dokument stehen.
_PV_JAHR = re.compile(r"Verf[uüi]{1,2}gung f[uü]r das Jahr\s+(\d{4})", re.IGNORECASE)
# Der Zeilenumbruch mitten im Wort („Prämi enverbilligung") überlebt das
# Plattmachen als Leerzeichen — deshalb hier nicht nach dem Wort suchen,
# sondern nach der Wendung davor.
_PV_BETRAG = re.compile(r"Höhe von\s+CHF\s+([\d'’.]+)", re.IGNORECASE)


def praemienverbilligung(text: str) -> list[Befund]:
    """Jahresanspruch aus einer IPV-Verfügung.

    Quelle ist OCR — der Befund gilt als unsicher und wird beim Import
    vorgelegt. Der Betrag selbst steht zwar meist sauber da, aber genau hier
    wäre ein verlesenes Zeichen teuer: aus 1'234.00 würde lautlos 123.40 mit
    verschobenem Komma.
    """
    f = flach(text)
    j = _PV_JAHR.search(f)
    b = _PV_BETRAG.search(f)
    if not j or not b:
        return []
    jahr = int(j.group(1))
    wert = betrag(b.group(1))
    if wert is None or wert <= 0:
        return []
    return [Befund(
        slug="kk_verbilligung",
        period_start=date(jahr, 1, 1),
        period_end=date(jahr, 12, 31),
        value=wert,
        unsicher=True,
        hinweis="Aus OCR-Text gelesen — bitte gegen die Verfügung prüfen.",
    )]


# ---------------------------------------------------------------------------
# Steuerrechnung (OCR-Text, am unzuverlässigsten)
# ---------------------------------------------------------------------------

_ST_JAHR = re.compile(r"(?:Kantons-\s*und\s*Gemeindesteuern|Bundessteuer)\s+(\d{4})",
                      re.IGNORECASE)
_ST_BUND = re.compile(r"Bundessteuer", re.IGNORECASE)
# Die zwei Nachkommastellen sind hier kein Schönheitsdetail, sondern der Filter:
# ohne sie las das Muster die Jahreszahl „2025" aus der Überschrift als Betrag.
# Schweizer Rechnungen führen den Betrag immer mit Rappen, auch wenn diese null sind.
_ST_BETRAG = re.compile(
    r"(?:Rechnungsbetrag|Steuerbetrag|Total)\s*(?:CHF)?\s+([\d'’]+\.\d{2})\b",
    re.IGNORECASE,
)


def steuerrechnung(text: str) -> list[Befund]:
    """Steuerjahr und Betrag aus einer Veranlagung oder Rechnung.

    Die schwächste Quelle im Bestand: diese PDFs sind Scans, deren Textebene aus
    OCR stammt und Zeichenfehler bis in Namen und Überschriften trägt
    („Ordentiiche Steuern"). Ein Betrag wird nur gemeldet, wenn Jahr **und**
    Betrag gefunden wurden — und immer als unsicher.

    Bewusst nicht versucht: provisorische von definitiver Veranlagung zu
    unterscheiden. Beide tragen dieselben Wörter, und eine geratene
    Unterscheidung wäre schlimmer als gar keine.

    **Eine 0.00 meldet dieser Weg nie**, obwohl es sie wirklich gibt. Jede
    Veranlagung führt mehrere „Total Steuerbetrag"-Zeilen — Bund, Kanton,
    Einwohner- und Kirchgemeinde —, deren Spaltenordnung beim Auslesen der
    Textebene verlorengeht. Aus dem Text allein ist nicht zu belegen, zu welcher
    Zeile eine gefundene Null gehört; sie als Kantonssteuer einzutragen wäre
    geraten. Echte Nullen kommen deshalb über die Ergänzungsdatei herein, wo sie
    jemand am Bild abgelesen und mit ``null_bestaetigt`` versehen hat.
    """
    f = flach(text)
    j = _ST_JAHR.search(f)
    b = _ST_BETRAG.search(f)
    if not j or not b:
        return []
    jahr = int(j.group(1))
    wert = betrag(b.group(1))
    if wert is None or wert <= 0:
        return []
    slug = "steuern_bund" if _ST_BUND.search(f) else "steuern_kanton"
    return [Befund(
        slug=slug,
        period_start=date(jahr, 1, 1),
        period_end=date(jahr, 12, 31),
        value=wert,
        unsicher=True,
        hinweis="Aus einem Scan gelesen — Betrag und Jahr bitte prüfen.",
    )]


# ---------------------------------------------------------------------------
# Mietvertrag
# ---------------------------------------------------------------------------

# Amtliches Formular: „<Nettomietzins> <Nebenkosten> <pauschal|akonto> <Total>",
# gefolgt von der Zeile „Total Mietzins und Nebenkosten". Der Abschlusstext ist
# der verlässliche Anker — die Ziffern davor sind Formularfelder ohne Beschriftung.
_MIETE_BETRAEGE = re.compile(
    r"CHF\s+([\d'’.]+)\s+CHF\s+([\d'’.]+)\s+(pauschal|akonto)\s+CHF\s+([\d'’.]+)"
    r"\s+Total Mietzins und Nebenkosten",
    re.IGNORECASE,
)
# Der Mietbeginn steht unmittelbar vor dem Mietzins-Block („1.Juni 2024 6 Mietzins").
_MIETE_BEGINN = re.compile(
    r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})\s+\d+\s+Mietzins", re.IGNORECASE
)
_MIETE_REFZINS = re.compile(r"Referenzzinssatz.{0,140}?(\d[.,]\d{1,2})\s*%", re.IGNORECASE)


def mietvertrag(text: str) -> list[Befund]:
    """Mietzins, Nebenkosten und Referenzzinssatz aus einem Mietvertrag.

    Der Wert ist die **Gesamtbelastung** (Nettomietzins + Nebenkosten), weil das
    der Betrag ist, der monatlich abgeht. Die Aufteilung steht in den Nebenwerten.

    Der Referenzzinssatz wandert mit, obwohl er kein Geldbetrag ist: fällt der
    landesweite Satz unter den im Vertrag festgehaltenen, entsteht ein Anspruch
    auf Mietzinssenkung. Ohne den Vertragsstand daneben lässt sich das nicht
    erkennen — und ein solcher Anspruch verjährt still.
    """
    f = flach(text)
    b = _MIETE_BETRAEGE.search(f)
    a = _MIETE_BEGINN.search(f)
    if not b or not a:
        return []
    beginn = _klartext_datum(*a.group(1, 2, 3))
    netto, neben, art, total = (
        betrag(b.group(1)), betrag(b.group(2)), b.group(3).lower(), betrag(b.group(4))
    )
    if beginn is None or netto is None or neben is None or total is None:
        return []
    # Gegenprobe: die drei Zahlen müssen zueinander passen. Tun sie das nicht,
    # hat das Muster Formularfelder erwischt, die nicht zusammengehören.
    if netto + neben != total:
        return []
    extras = {"nettomietzins": str(netto), "nebenkosten": str(neben), "nk_art": art}
    r = _MIETE_REFZINS.search(f)
    if r is not None and (satz := betrag(r.group(1).replace(",", "."))) is not None:
        extras["referenzzinssatz"] = str(satz)
    return [Befund(
        slug="miete", period_start=beginn, period_end=beginn, value=total, extras=extras,
    )]


# ---------------------------------------------------------------------------
# Leistungsabrechnung (selbst getragene Gesundheitskosten)
# ---------------------------------------------------------------------------

# Zwei Fassungen: ältere Abrechnungen schreiben „Franchise 375.10", neuere
# „Franchise CHF 54.10" — das Währungskürzel ist darum optional.
#
# Zwei Sicherungen gegen dieselbe Falle. „Ihre Jahresfranchise 2020 CHF 2'500.00"
# darf nicht als angerechneter Anteil gelesen werden: der Blick zurück schliesst
# das Wort aus, und die geforderten zwei Nachkommastellen verhindern, dass
# stattdessen die Jahreszahl 2020 als Betrag durchgeht. Genau dieser Fehler war
# beim Steuer-Parser schon einmal da.
_LA_FRANCHISE = re.compile(r"(?<!Jahres)Franchise\s+(?:CHF\s+)?([\d'’]+\.\d{2})")
_LA_SELBSTBEHALT = re.compile(r"Selbstbehalt\s+(?:CHF\s+)?([\d'’]+\.\d{2})")
_LA_JAHRESFRANCHISE = re.compile(r"Jahresfranchise\s+(\d{4})\s+CHF\s+([\d'’.]+)")
_LA_BEHANDLUNG = re.compile(r"Behandlung vom\s+(\d{2}\.\d{2}\.\d{4})")


def leistungsabrechnung(text: str) -> list[Befund]:
    """Selbst getragener Anteil einer Behandlung, als Jahresposten.

    Der Wert wird aus **Franchise + Selbstbehalt** gebildet, nicht aus der Spalte
    „Ihr Anteil". Beide Wege führen zum selben Betrag, aber die Summe der beiden
    Bestandteile prüft sich selbst: stimmt sie nicht mit der ausgewiesenen Spalte
    überein, hat das Muster etwas Falsches gegriffen. Die Spaltenwerte dagegen
    stehen ohne Beschriftung nebeneinander und sind allein an ihrer Reihenfolge
    zu erkennen — ein Layoutwechsel würde dort lautlos die falsche Zahl liefern.

    ``additiv``: eine Abrechnung ist eine Behandlung. Die Jahressumme entsteht
    erst, wenn alle Abrechnungen des Jahres zusammengezählt werden.
    """
    f = flach(text)
    jf = _LA_JAHRESFRANCHISE.search(f)
    bh = _LA_BEHANDLUNG.search(f)
    if not jf and not bh:
        return []
    jahr = int(jf.group(1)) if jf else (datum(bh.group(1)) or date(1900, 1, 1)).year
    if jahr < 2000:
        return []

    franchise = Decimal("0")
    if (m := _LA_FRANCHISE.search(f)) is not None and (w := betrag(m.group(1))) is not None:
        franchise = w
    selbstbehalt = Decimal("0")
    if (m := _LA_SELBSTBEHALT.search(f)) is not None and (w := betrag(m.group(1))) is not None:
        selbstbehalt = w
    eigen = franchise + selbstbehalt
    if eigen <= 0:
        # Eine Abrechnung, bei der die Kasse alles trägt — korrekt gelesen,
        # aber für einen Kostenverlauf ohne Aussage.
        return []
    extras = {"franchise": str(franchise), "selbstbehalt": str(selbstbehalt)}
    if jf and (grenze := betrag(jf.group(2))) is not None:
        extras["jahresfranchise"] = str(grenze)
    return [Befund(
        slug="gesundheit_selbst",
        period_start=date(jahr, 1, 1),
        period_end=date(jahr, 12, 31),
        value=eigen,
        extras=extras,
        additiv=True,
    )]


# ---------------------------------------------------------------------------
# Lohnausweis (Nebenverdienste)
# ---------------------------------------------------------------------------

# Der Nettolohn traegt im Formular eine dreisprachige Beschriftung, danach folgt
# der Betrag und dann der Hinweis „In die Steuererklaerung uebertragen". Genau
# die LETZTE Zahl davor ist der Nettolohn — dazwischen stehen je nach Formular
# noch Ziffernummern („10 Berufliche Vorsorge"), die keine Betraege sind.
# OCR macht aus dem „In" gelegentlich ein „ln"; beides wird akzeptiert.
_LA_NETTO_BLOCK = re.compile(
    r"Nettolohn.{0,120}?((?:[\d'’´]+[.,]?\d*\s*)+)[lI]n die Steuererkl",
    re.IGNORECASE | re.DOTALL,
)
_LA_ZAHL = re.compile(r"[\d'’´]+(?:[.,]\d{2})?")
# Zwei Daten hintereinander: Beginn und Ende der Lohnperiode (Felder E und F).
_LA_PERIODE = re.compile(r"(\d{2}\.\d{2}\.(\d{4}))\s+(\d{2}\.\d{2}\.\d{4})")


def lohnausweis(text: str) -> list[Befund]:
    """Nettolohn eines Lohnausweises — als Nebenverdienst.

    **Bewusst eine eigene Reihe, nicht ``lohn``.** Die Reihe ``lohn`` fuehrt den
    AHV-Jahreslohn aus dem Vorsorgeausweis, also den Jahreslohn der Hauptstelle.
    Ein Lohnausweis einer Nebenbeschaeftigung nennt eine voellig andere Groesse.
    Beides in dieselbe Kurve gestellt, behauptet sie einen Absturz, den es nie
    gab — zwei verschiedene Groessen in einer Reihe sind schlimmer als eine
    fehlende.

    (Hier standen Frueher die tatsaechlichen Groessenordnungen. Das
    war eine Angabe ueber den Besitzer dieser Anlage, nicht ueber den Parser —
    in einem oeffentlichen Repository hat sie nichts zu suchen. Der technische
    Grund fuer die getrennte Reihe steht oben und kommt ohne sie aus.)

    ``additiv``: in einem Jahr koennen mehrere Arbeitgeber je einen Ausweis
    ausstellen. Der Jahreswert ist ihre Summe, nicht der eines beliebigen davon.

    Gelesen wird der NETTOLOHN (Ziffer 11), nicht der Bruttolohn: er ist der
    Betrag, der tatsaechlich aufs Konto kam, und damit die Groesse, die sich mit
    den Buchungen vergleichen laesst.
    """
    f = flach(text)
    periode = _LA_PERIODE.search(f)
    block = _LA_NETTO_BLOCK.search(f)
    if not periode or not block:
        return []
    jahr = int(periode.group(2))
    zahlen = _LA_ZAHL.findall(block.group(1))
    if not zahlen:
        return []
    netto = betrag(zahlen[-1])
    if netto is None or netto <= 0:
        return []
    return [Befund(
        slug="nebenverdienst",
        period_start=date(jahr, 1, 1),
        period_end=date(jahr, 12, 31),
        value=netto,
        additiv=True,
    )]


# ---------------------------------------------------------------------------
# Anbieter-Rechnung — der einzige Beleg, der nach Positionen aufgeschlüsselt wird
# ---------------------------------------------------------------------------

# Präfix der Positions-Schlüssel in ``MetricPoint.extras``. Die Konvention steht
# am Modell; hier nur der Schlüssel selbst, damit Parser, Skript und Import
# dieselbe Zeichenkette benutzen und nicht drei Schreibweisen entstehen.
POS_PRAEFIX = "pos:"
# Ausgewiesene Rundungsdifferenz der Rechnung. Sie ist KEINE Position — sie
# entsteht erst beim Runden der Gesamtsumme auf fünf Rappen. Als ``pos:`` geführt
# stünde sie in einem gestapelten Balken als eigener Posten da, den es nicht gibt.
POS_RUNDUNG = "rundung"
# Summe aller Rabatte, positiv geschrieben. Abgeleitet aus den Positionen und
# trotzdem eigens abgelegt: sie ist der Nebenwert der Reihe, und die
# Verlaufsseite liest Nebenwerte über einen einzelnen Schlüssel.
POS_RABATT = "rabatt"




# Ein Betrag der Positionstabelle: immer mit zwei Nachkommastellen. Die Menge
# („1") und eine Vertragsnummer sähen sonst genauso aus wie ein Betrag. Das ist
# KEINE Anbietersache und steht darum weiter hier.
_BETRAG_ZELLE = re.compile(r"^-?[\d'’´]+\.\d{2}$")


class PruefsummeFehler(ValueError):
    """Die Positionen einer Rechnung ergeben nicht den Rechnungsbetrag.

    Eine eigene Ausnahme statt einer leeren Liste: eine Rechnung, deren
    Positionen nicht aufgehen, ist nicht „nichts gefunden" — sie ist falsch
    gelesen. Der Unterschied muss im Lauf sichtbar sein, sonst fehlte am Ende
    ein Monat im Verlauf, ohne dass jemand erführe, warum.
    """


def _sc_letzter_tag_im_monat(jahr: int, monat: int) -> date:
    """Letzter Tag des Monats."""
    return _letzter_tag(date(jahr, monat, 1))


def rechnung_nach_profil(text: str, profil: Anbieterprofil) -> list[Befund]:
    """Monatsrechnung mit einzeln aufgeschlüsselten Positionen — Layout aus dem Profil.

    **Der Anbieter ist hier Daten, nicht Code.** Diese Funktion las früher EIN
    bestimmtes Rechnungslayout und trug den Namen des Anbieters. Damit war jeder
    weitere Anbieter eine Code-Änderung — und der Quelltext verriet, bei wem der
    Autor Kunde ist. Was das Layout ausmacht (Anker, Abschnitte, Endzeilen, die
    drei Muster) steht jetzt in einer ``.toml``-Datei; siehe
    :mod:`moneten.services.anbieter_profil`.

    Was NICHT ins Profil gehört, steht weiter hier: dass ein Betrag zwei
    Nachkommastellen hat, dass die letzte Zelle der Betrag ist, und die
    Selbstprüfung. Das gilt für jede Rechnung dieser Bauart.

    **Erwartet Spaltentext, nicht Lesereihenfolge.** Je Tabellenzeile eine
    Textzeile, die Zellen durch Tabulator getrennt. Der Grund ist gemessen: die
    Spalten Menge / Preis pro Einheit / Betrag sind je nach Position
    unterschiedlich besetzt. „Paket S 1 5.00 5.00" und
    „Rabatt Paket S 1 -5.00" sehen in der Lesereihenfolge gleich aus,
    meinen aber Verschiedenes — im ersten Fall ist die zweite Zahl der Preis, im
    zweiten der Betrag. Mit Spalten ist die letzte Zelle immer der Betrag.

    Der Zeitraum („01.08.24 - 30.08.24") steht zwischen Menge und Beträgen und
    wird schon beim Extrahieren entfernt; sonst klebte er als eigene Zelle
    dazwischen und verschöbe die Zählung.

    **Die Selbstprüfung ist der eigentliche Wert dieser Funktion.** Positionen
    einzeln zu lesen heisst, sich in einem Dutzend Zeilen irren zu können, ohne
    dass die Summe es verrät. Darum: Summe aller Positionen plus ausgewiesene
    Rundungsdifferenz muss den Rechnungsbetrag ergeben, sonst
    :class:`PruefsummeFehler`. Ein stiller Zahlendreher wäre schlimmer als ein
    fehlender Monat — die Kurve behauptete etwas, das nie auf der Rechnung stand.

    **Was keine Rechnung ist, liefert nichts.** Neben Rechnungen liegen oft
    Nutzungsnachweise: Belege, die jede einzelne Nutzung aufführen und dabei
    fast durchgehend 0.00 ausweisen, weil sie im Abonnement enthalten war. Sie
    belegen keine Zahlung und gehören in keine Kostenreihe. Erkannt werden sie
    am fehlenden Rechnungskopf — die Muster ``monat`` und ``total`` des Profils
    treffen beide nur auf einer Rechnung zu. Das ist bewusst eine Prüfung am
    INHALT und nicht am Dateinamen: Dateien werden umbenannt und Ordner
    umsortiert, ein Rechnungskopf nicht.

    Die Positionen landen als ``pos:<Name>`` in den Nebenwerten; die Konvention
    steht bei :class:`~moneten.db.models.MetricPoint`.
    """
    f = flach(text)
    m = profil.monat.search(f)
    t = profil.total.search(f)
    if not m or not t:
        return []
    monat_nr = _MONATE.get(m.group(1).strip().lower())
    total = betrag(t.group(1))
    if monat_nr is None or total is None:
        return []
    jahr = int(m.group(2))

    rundung = Decimal("0")
    if (profil.rundung is not None and (r := profil.rundung.search(f)) is not None
            and (w := betrag(r.group(1))) is not None):
        rundung = w

    positionen = _positionen_nach_profil(text, profil)
    if not positionen:
        return []

    summe = sum(positionen.values(), Decimal("0"))
    if summe + rundung != total:
        raise PruefsummeFehler(
            f"Rechnung {m.group(1)} {jahr}: {len(positionen)} Positionen ergeben "
            f"zusammen nicht den Rechnungsbetrag (Abweichung "
            f"{summe + rundung - total})."
        )

    extras = {f"{POS_PRAEFIX}{name}": str(wert) for name, wert in positionen.items()}
    if rundung:
        extras[POS_RUNDUNG] = str(rundung)
    rabatt = -sum((w for w in positionen.values() if w < 0), Decimal("0"))
    if rabatt:
        extras[POS_RABATT] = str(rabatt)

    return [Befund(
        slug=profil.slug,
        period_start=date(jahr, monat_nr, 1),
        period_end=_sc_letzter_tag_im_monat(jahr, monat_nr),
        value=total,
        extras=extras,
    )]


def _positionen_nach_profil(text: str, profil: Anbieterprofil) -> dict[str, Decimal]:
    """Positionsname → Betrag, in der Reihenfolge der Rechnung.

    Gleichnamige Positionen werden addiert: dasselbe Abonnement kann auf zwei
    Verträgen stehen, und für einen gestapelten Balken ist das ein Posten. Ein
    zweiter Schlüssel mit angehängter Ziffer sähe im Verlauf aus wie ein neues
    Produkt, das es nie gab.
    """
    positionen: dict[str, Decimal] = {}
    gefunden_anker = False
    abschnitt: str | None = None

    for zeile in text.splitlines():
        zellen = [z.strip() for z in zeile.split("\t")]
        kopf = zellen[0]
        if not gefunden_anker:
            gefunden_anker = kopf == profil.anker
            continue
        if kopf in profil.abschnitte:
            abschnitt = kopf
            continue
        if kopf.startswith(profil.ende):
            abschnitt = None
            continue
        if abschnitt is None or len(zellen) < 2:
            # Zeilen ohne Abschnitt sind Anschrift, Vertragskopf oder der
            # gesperrte Auslandsblock. Zeilen mit nur einer Zelle sind
            # Zwischenüberschriften wie „Im Ausland abgehend" — sie tragen
            # keinen Betrag und beenden den Abschnitt ausdrücklich NICHT,
            # sonst fielen die Positionen darunter aus der Summe.
            continue
        wert = betrag(zellen[-1]) if _BETRAG_ZELLE.match(zellen[-1]) else None
        name = profil.laufzeit.sub("", kopf) if profil.laufzeit else kopf
        if wert is None or not name:
            continue
        positionen[name] = positionen.get(name, Decimal("0")) + wert
    return positionen


# Welcher Parser für welchen Beleg. Die Zuordnung erfolgt über den Ordnernamen
# und ein Stichwort im Dateinamen — das ist stabiler als Textschnüffeln, weil
# die Scans konsequent benannt sind.
PARSER = {
    "kk_praemie": praemienabrechnung,
    "strom": stromrechnung,
    "pk": vorsorgeausweis,
    "police": police,
    "hausrat": hausrat_police,
    "verbilligung": praemienverbilligung,
    "steuern": steuerrechnung,
    "miete": mietvertrag,
    "leistung": leistungsabrechnung,
    "lohnausweis": lohnausweis,
}

# Anbieterprofile: je Profil ein Eintrag in PARSER, ohne eine Zeile Code.
# Geladen wird beim Import — die Dateien aendern sich zur Laufzeit nicht, und
# ein Profil je Beleg zu lesen waere Verschwendung.
PROFILE = lade_profile(getattr(settings, "anbieter_dir", None))
for _profil in PROFILE.values():
    PARSER[_profil.slug] = partial(rechnung_nach_profil, profil=_profil)
    _MINDESTWERT.setdefault(_profil.slug, _profil.toleranz)

# Diese Parser bekommen Spaltentext statt Lesereihenfolge. Das Skript schlägt
# hier nach, welche Extraktion ein Beleg braucht — sonst müsste es den
# Sonderfall selbst kennen und die beiden Listen driften auseinander.
# Belege, die als SPALTENTEXT gelesen werden muessen (je Tabellenzeile eine
# Textzeile, Zellen durch Tabulator). Das ergibt sich aus der Bauart der
# Rechnung, also aus dem Profil — nicht aus einer zweiten Liste, die man
# vergessen kann.
SPALTENTEXT = frozenset(PROFILE)
