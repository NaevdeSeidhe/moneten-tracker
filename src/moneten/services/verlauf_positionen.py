"""Eine Verlaufsreihe mit Positionen als gestapelter Balken je Periode.

Der Rechnungsbetrag allein sagt nicht, WARUM er steigt. Eine ausgelaufene
Promotion sieht darin genauso aus wie ein teureres Abonnement oder ein einmalig
gekauftes Gerät. Die Positionen stehen seit dem Beleg-Parser in
``MetricPoint.extras`` (Konvention am Modell, ``pos:<Name>``); dieser Dienst
macht daraus die Geometrie eines Balkendiagramms.

**Aufbau des Bildes.**

* ÜBER der Nulllinie stehen die positiven Positionen, gestapelt.
* UNTER der Nulllinie stehen die Rabatte, ebenfalls gestapelt. Sie werden
  schraffiert gezeichnet — dass ein Balken nach unten zeigt, sagt für sich noch
  nicht, dass er abgezogen wird.
* Eine gerade Linie über den Balken zeigt den **tatsächlich bezahlten** Betrag
  je Periode (``MetricPoint.value``). Das ist die Zahl, um die es geht, und aus
  den Balken allein ist sie nicht ablesbar: sie ist brutto minus Rabatte.

**Eine Skala für oben und unten.** Die beiden Zeilen des Diagramms bekommen ihre
Höhe im Verhältnis ihrer beiden Achsendeckel. Nur so bedeutet ein Millimeter
oben denselben Betrag wie ein Millimeter unten. Zwei eigene Skalen wären
bequemer — ein kleiner Rabatt bliebe immer gut sichtbar — und wären eine Lüge:
ein Rabatt von 12 Franken sähe dann aus wie eine Rechnung von 120.

**Die Achse richtet sich nach den LAUFENDEN Perioden.** Ein einmalig gekauftes
Gerät ist ein Vielfaches einer Monatsrechnung. Nimmt die Achse dessen Höhe, wird
jeder laufende Monat zu einem Strich, und die Frage, um die es geht, ist am Bild
nicht mehr zu beantworten. Der Deckel (:func:`deckel`) ist darum ein robuster
oberer Wert der Reihe; was darüber liegt, wird bis zur Decke gezeichnet, dort
sichtbar abgeschnitten und BENANNT (:class:`Kappung`) — mit dem Positionsnamen,
den der Parser wörtlich von der Rechnung liest, und dem Betrag.

Eine gestauchte Achse (logarithmisch o.ä.) täte das nicht: in einem Stapelbalken
müssen sich die Bänder zur Balkenhöhe addieren, und auf einer gestauchten Achse
tun sie das nicht mehr. Das Bild zeigte dann eine Zusammensetzung, die es nicht
gibt.

**Farbe hängt am NAMEN, nicht am Platz im Stapel.** Dieselbe Position muss über
alle Jahre dieselbe Farbe tragen, sonst ist „was kommt dazu, was fällt weg"
nicht ablesbar. Die Zuordnung entsteht aus der alphabetisch sortierten Liste der
Namen — nicht aus einem Hash: der springt, sobald eine Position dazukommt, und
färbt rückwirkend die ganze Reihe um.

**Warum die Geometrie hier und nicht im Template entsteht:** wie bei Treemap,
Sankey und Steuer-Übersicht. Prozentwerte im Template sind nicht nachrechenbar;
hier sind sie es. Alle Prozentwerte sind Bildschirmarithmetik und darum float —
jeder Betrag, der ANGEZEIGT wird, bleibt Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from moneten.dates import add_months, heute_lokal
from moneten.db.models import MetricCadence, MetricUnit
from moneten.palette import chart_colors

# Die Präfixe kommen aus dem Modul, das sie SCHREIBT. Eine zweite Konstante für
# dieselbe Konvention liefe beim ersten Umbenennen auseinander — und der Balken
# stünde danach leer da, ohne dass irgendwo ein Fehler entstünde.
from moneten.services.belege_parser import POS_PRAEFIX, POS_RUNDUNG
from moneten.services.metrics import Punkt, Verlauf, formatiere, periode_text

# Wie viele Positionsnamen eine eigene Farbe bekommen: genau so viele, wie die
# Palette Töne hat. Eine neunte Farbe gäbe es nur durch Wiederholung, und zwei
# gleichfarbige Bänder im selben Balken sind schlimmer als ein ehrlich benannter
# Sammelposten. Ausgewählt werden die grössten — die kleinen fallen in „Übrige",
# ihre Namen und Beträge stehen weiterhin im Aufklapper jeder Periode.
MAX_POSTEN = len(chart_colors())

# Sammelposten, Rundung und unaufgeschlüsselte Perioden bleiben unbunt: nichts
# davon ist ein Posten der Rechnung.
#
# ``--text-secondary`` und NICHT ``--text-tertiary``, obwohl die Sammelreihe des
# Vermögens-Verlaufs letzteres nimmt. Dort ist es eine LINIE neben anderen
# Linien, hier ein BAND im selben Stapel wie die Palettenfarben — und im Browser
# nachgemessen liegt ``--text-tertiary`` in „synthwave" nur dE 6.7 und in
# „melange" 7.5 von ``--chart-7`` entfernt, also unter den 8.5, die sich die
# Palette selbst als Mindestabstand gibt. Der Sammelposten sähe dort aus wie
# eine Position. ``--text-secondary`` hält über alle sechs Skins mindestens
# dE 16.1 und 5.66:1 gegen die Karte; tests/test_chart_kontrast.py rechnet es
# nach.
REST_FARBE = "var(--text-secondary)"
REST_NAME = "Übrige"

# Beschriftung der Rundungsdifferenz im Aufklapper. Sie ist KEINE Position (so
# schreibt es der Parser) und bekommt darum auch kein Band im Balken — als
# eigener Posten stünde dort ein Rappenbetrag, den es als Leistung nicht gibt.
# Im Aufklapper muss sie stehen, sonst ergeben die Zeilen nicht den Betrag, der
# darunter als bezahlt ausgewiesen ist.
RUNDUNG_NAME = "Rundung"

# Schlüssel der Bezahlt-Zeile in ``Balken.zeilen``. Leer, weil ein Positionsname
# nie leer ist (:func:`positionen` wirft leere Namen weg) — damit kann keine
# Position die Summenzeile überschreiben.
BEZAHLT = ""

# Mindesthöhe der Rabatt-Zeile, als Anteil der Balkenfläche. Sie ist eine
# LAYOUT-Grenze und keine Skalenkorrektur: die Balken darin behalten dieselben
# Pixel je Franken wie oben, die Zeile bekommt bloss Luft. Ohne sie fiel die
# Zeile in einem Monat mit einem einmaligen Grossposten auf wenige Pixel
# zusammen — die Achsenbeschriftung hätte dort keinen Platz mehr und stünde in
# den Periodenangaben darunter. Die Rabatt-Balken bleiben in so einem
# Monat winzig; das ist die Wahrheit und nicht zu beheben, aber ihre
# Beschriftung darf trotzdem nicht ins Nachbarelement rutschen.
MIN_UNTEN = 0.12

# Mindesthöhe der OBEREN Zeile, Gegenstück zu ``MIN_UNTEN``. Sie greift, wenn
# über der Nulllinie nichts steht — eine Periode, deren Rechnung nur aus
# Gutschriften besteht. Ohne sie war die obere Zeile 0 px hoch: die Nulllinie
# lag am oberen Rand des Zeichenfelds, und ihre „0" stand auf derselben
# Grundlinie wie die Zahl der Rabattzeile. Gemessen waren es drei Achsenzahlen
# ineinander über einem leeren Rechteck.
MIN_OBEN = 0.12

# Unter so wenigen Perioden gibt es keinen „laufenden" Fall, gegen den ein
# Ausreisser einer wäre — dann bleibt die Achse beim Höchstwert.
MIN_PERIODEN_DECKEL = 5

# Der Deckel ist das grössere von 90. Perzentil und Median mal Faktor. Das
# Perzentil trägt eine Reihe, die auf ein neues Niveau springt (die halbe Reihe
# liegt oben, das Perzentil auch); der Median mal Faktor trägt eine Reihe, die um
# ihren Normalwert schwankt und deren Perzentil zu knapp über dem Median läge.
DECKEL_FAKTOR = Decimal("1.5")

# Abgeschnitten wird erst, wenn der Höchstwert den Deckel um diesen Faktor
# überragt. Eine Schnittmarke ist ein starkes Zeichen; für ein Fünftel Unter-
# schied wäre sie Lärm, und die Reihe verlöre ihre echte Obergrenze ohne Not.
DECKEL_SCHWELLE = Decimal("1.5")

# Marke eines unbestätigten Punktes in ``extras``. Dieselbe, die
# ``routers/metrics.py`` setzt und das Template liest.
_UNSICHER = "unsicher"


@dataclass(frozen=True)
class Posten:
    """Ein Positionsname über die ganze Reihe — Legenden- und Aufklapper-Zeile."""

    name: str
    farbe: str
    # Kommt NEGATIV vor und nie positiv: dann gehört das Legendenmuster
    # schraffiert, weil dieser Posten nie über der Nulllinie zu sehen ist. Die
    # erste Bedingung ist keine Feinheit — eine Position, die in jeder Periode
    # mit 0.00 auf der Rechnung steht, kommt ebenfalls nie positiv vor und trug
    # damit das Muster mit der Bedeutung „wird abgezogen".
    nur_rabatt: bool
    # Stelle im Werte-Kasten, an der diese Zeile steht. Ein FORTLAUFENDER Index
    # und nicht der Name: eine Position, die zufällig „Rundung" heisst, teilte
    # sich sonst Zeile und Betrag mit der ausgewiesenen Rundungsdifferenz —
    # gemessen fiel dabei ein Positionsbetrag aus dem Kasten, und die Zeilen
    # ergaben nicht mehr den bezahlten Betrag.
    schluessel: str = ""


@dataclass(frozen=True)
class Zeile:
    """Eine Zeile des Werte-Kastens: wohin sie gehört, was sie sagt."""

    schluessel: str
    name: str
    betrag: str


@dataclass(frozen=True)
class Segment:
    """Ein Band eines Stapels."""

    name: str
    farbe: str
    betrag: Decimal  # immer positiv — die Richtung steckt im Stapel
    anteil: float  # Höhe in Prozent des eigenen Stapels
    rabatt: bool


@dataclass(frozen=True)
class Kappung:
    """Was an einer Periode nicht ins Bild passt — benannt statt gezeichnet.

    Ein Balken, der bloss an der Decke endet, wäre eine Lüge über seine Höhe.
    Darum steht neben der Schnittmarke, WAS oberhalb liegt: der Positionsname,
    wie der Parser ihn von der Rechnung liest, und der Betrag.
    """

    label: str
    # Bänder, die an der Decke abgeschnitten sind — Name und voller Betrag.
    posten: list[tuple[str, str]]
    # Dasselbe unter der Nulllinie. EIN Eintrag je Periode und nicht zwei: mit je
    # einem Eintrag oben und unten stand dieselbe Periodenangabe zweimal
    # untereinander und las sich wie ein Fehler.
    posten_unten: list[tuple[str, str]]
    # Gesetzt, wenn die Bezahlt-Marke selbst ausserhalb der Achse liegt: bei
    # einem negativ bezahlten Monat (Gutschrift) fiel der Punkt aus dem
    # Zeichenfeld und die Linie brach ab, ohne dass etwas den Grund nannte.
    bezahlt: str | None


@dataclass(frozen=True)
class Balken:
    """Eine Periode: zwei Stapel und ein Punkt auf der Bezahlt-Linie."""

    punkt_id: int
    label: str
    brutto: Decimal
    rabatt: Decimal
    bezahlt: Decimal
    oben: list[Segment]
    unten: list[Segment]
    h_oben: float  # Höhe in Prozent der oberen Zeile
    h_unten: float  # Höhe in Prozent der unteren Zeile
    x: float  # Spaltenmitte in Prozent
    y: float  # Bezahlt-Linie in Prozent der oberen Zeile (0 = oben)
    # Keine Positionen erfasst (von Hand nachgetragener Wert): die Höhe ist
    # bekannt, die Zusammensetzung nicht. Wird offen gezeichnet statt gefüllt —
    # ein gefülltes Band behauptete eine Position, die niemand kennt.
    offen: bool
    unsicher: bool
    # Die Zeilen des Werte-Kastens in Kastenreihenfolge, zuletzt ``BEZAHLT``.
    # Fertig formatiert; im Browser wird nichts gerechnet.
    zeilen: list[Zeile]
    # An der Achsendecke abgeschnitten (oben bzw. unten). Der Balken bekommt
    # dort eine Schnittmarke — sonst behauptete seine Kante eine Höhe, die er
    # nicht hat.
    gekappt: bool = False
    gekappt_unten: bool = False
    # Die Bezahlt-Marke liegt ausserhalb der Achse und sitzt geklemmt am Rand.
    y_aus: bool = False
    # Periode ohne Wert: angehaengt, damit die Achse bis heute reicht. Sie
    # zeichnet keinen Stapel, traegt keinen Punkt auf der Bezahlt-Linie und
    # unterbricht sie — eine Linie durch eine leere Periode behauptete dort
    # einen Betrag von null, und null ist etwas anderes als „nicht erfasst".
    leer: bool = False


@dataclass(frozen=True)
class Bild:
    """Render-Modell des Balkendiagramms."""

    balken: list[Balken]
    # Legende: die farbtragenden Posten, dahinter „Übrige" und „Rundung".
    posten: list[Posten]
    # Jede Zeile, die ein Aufklapper zeigen kann — auch die Namen, die im Balken
    # in „Übrige" zusammengefasst sind. Das Markup rendert sie einmal, das
    # Skript blendet je Periode ein, was dort vorkommt: so steht kein
    # Positionsname jemals als vom Skript zusammengebauter HTML-Text da.
    zeilen_vorlage: list[Posten]
    # Obergrenze der Achse — NICHT zwingend der Höchstwert der Reihe, siehe
    # :func:`deckel`. Genau diese Zahl steht an der Achse.
    deckel: Decimal
    deckel_unten: Decimal
    # Anteil der oberen Zeile an der gesamten Balkenfläche, in Prozent.
    anteil_oben: float
    # Wo in der Rabattzeile ihre Achsenzahl steht, in Prozent von deren Höhe. In
    # der Regel 100 — kleiner, sobald ``MIN_UNTEN`` der Zeile Luft gegeben hat.
    # Die Beschriftung hängt daran, sonst benennte sie den Zeilenrand statt den
    # Wert. Die OBERE Zeile braucht das nicht: ihre Achse ist die Bezugsgrösse,
    # ihr Balken erreicht die Decke immer (siehe ``skala``).
    marke_unten: float
    # Perioden, die über die Achse hinausreichen — je eine Zeile unter dem Bild.
    kappungen: list[Kappung]
    # Bezahlt-Linie als SVG-Pfad. GERADE Segmente, keine Glättung: zwischen zwei
    # Rechnungen gibt es keine Zwischenwerte, eine Kurve behauptete welche.
    pfad: str
    zeigt_oben: bool
    zeigt_rabatt: bool
    hat_offene: bool
    hat_unsichere: bool

    @property
    def nach_punkt(self) -> dict[int, Balken]:
        """Punkt-ID → Balken. Die Werteliste hängt die Positionen daran.

        Sie muss sie zeigen: das Diagramm trägt als ``aria-label`` die Zusage,
        alle Zahlen stünden in der Liste darunter. Mit Positionen im Bild und
        nur Summen in der Liste wäre diese Zusage falsch.
        """
        return {b.punkt_id: b for b in self.balken}

    @property
    def xmarken(self) -> list[tuple[float, str]]:
        """Bis zu fuenf Achsenmarken, gleichmaessig ueber die Balken verteilt.

        Aus den Balken selbst und nicht aus einer Datumsrechnung: die Spalten
        sind gleich breit, also IST die Balkennummer die Position. Eine
        Zeitachse darueberzulegen verschoebe die Marken gegen die Balken, sobald
        eine Periode fehlt.

        Vorher standen hier zwei Angaben, erste und letzte. Zwischen ihnen lagen
        bis zu vierundzwanzig Balken ohne eine einzige Marke.
        """
        n = len(self.balken)
        if n <= 1:
            return [(b.x, b.label) for b in self.balken]
        schritt = max(1, -(-n // MAX_XMARKEN))
        gewaehlt = list(range(0, n, schritt))
        if gewaehlt[-1] != n - 1:
            gewaehlt.append(n - 1)
        if len(gewaehlt) > MAX_XMARKEN:
            gewaehlt = gewaehlt[:-2] + [n - 1]
        return [(self.balken[i].x, self.balken[i].label) for i in gewaehlt]

    @property
    def letzter_wert(self) -> Balken | None:
        """Der letzte Balken MIT Wert.

        Die Marke auf der Bezahlt-Linie gehoert dorthin und nicht auf den
        letzten Balken ueberhaupt: seit die Achse bis heute reicht, ist der oft
        eine leere Periode, und die Marke saesse am Boden — also auf „null
        bezahlt".
        """
        for b in reversed(self.balken):
            if not b.leer:
                return b
        return None

    def als_json(self) -> list[dict]:
        """Was ``app.js`` je Periode braucht — Beträge fertig formatiert.

        Im Browser gäbe es weder Decimal noch das Schweizer Apostroph umsonst.
        Gerechnet wird dort nichts; der Werte-Kasten ordnet nur zu.
        """
        return [
            {"label": b.label, "x": b.x, "y": b.y, "aus": b.y_aus, "leer": b.leer,
             "zeilen": {z.schluessel: z.betrag for z in b.zeilen}}
            for b in self.balken
        ]


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


def _dezimal(roh: object) -> Decimal | None:
    """Text zu ``Decimal`` — ``None``, wenn daraus keine endliche Zahl wird."""
    try:
        wert = Decimal(str(roh))
    except (ArithmeticError, ValueError, InvalidOperation):
        return None
    return wert if wert.is_finite() else None


def positionen(punkt: Punkt) -> dict[str, Decimal]:
    """Positionsname → Betrag eines Punktes (negativ = Rabatt).

    Der Name darf selbst Doppelpunkte enthalten, getrennt wird am ersten — genau
    so schreibt der Parser sie (Konvention am Modell ``MetricPoint``).

    Ein unlesbarer Betrag wird ÜBERSPRUNGEN statt als Ausnahme geworfen. Der
    Import lässt so etwas gar nicht erst durch (``_extras_lesen``); käme es
    trotzdem vor — von Hand in der DB geändert —, wäre eine Fehlerseite beim
    Zeichnen die schlechtere Antwort als ein Balken, der um diesen Posten zu
    kurz ist und dadurch sichtbar von der Bezahlt-Linie abweicht.
    """
    out: dict[str, Decimal] = {}
    for schluessel, roh in (punkt.extras or {}).items():
        if not schluessel.startswith(POS_PRAEFIX):
            continue
        name = schluessel[len(POS_PRAEFIX):].strip()
        wert = _dezimal(roh)
        if not name or wert is None:
            continue
        out[name] = out.get(name, Decimal("0")) + wert
    return out


def rundung(punkt: Punkt) -> Decimal:
    """Ausgewiesene Rundungsdifferenz — ``0``, wenn keine erfasst ist."""
    roh = (punkt.extras or {}).get(POS_RUNDUNG)
    return Decimal("0") if roh is None else (_dezimal(roh) or Decimal("0"))


def hat_positionen(v: Verlauf) -> bool:
    """Trägt wenigstens ein Punkt dieser Reihe Positionen?"""
    return any(positionen(p) for p in v.punkte)


# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------


def farbzuordnung(gewicht: dict[str, Decimal]) -> dict[str, str]:
    """Positionsname → CSS-Variable.

    Zwei Schritte, die verschiedene Fragen beantworten:

    1. WELCHE Namen eine eigene Farbe bekommen, entscheidet ihr Gewicht (Summe
       der Beträge über die ganze Reihe). Die Palette hat acht Töne; die
       grössten Posten prägen das Bild und müssen unterscheidbar bleiben.
    2. WELCHE Farbe sie bekommen, entscheidet die alphabetische Reihenfolge. Die
       Zuordnung ist damit ohne Kenntnis der Beträge nachvollziehbar. Über einen
       Hash wäre sie es nicht — und eine neue Position würfelte die bestehenden
       Farben durcheinander.

    Die STAPELREIHENFOLGE hängt nicht hieran (siehe :func:`positions_bild`): sie
    richtet sich nach der Höhe, die eine Position in einer einzelnen Periode
    erreicht, weil an der Achsendecke von oben abgeschnitten wird.
    """
    gross = sorted(gewicht, key=lambda n: (-gewicht[n], n))[:MAX_POSTEN]
    palette = chart_colors()
    return {name: palette[i] for i, name in enumerate(sorted(gross))}


# ---------------------------------------------------------------------------
# Bild
# ---------------------------------------------------------------------------


def _aufrunden(wert: Decimal) -> Decimal:
    """Auf zwei geltende Stellen aufgerundet — die Zahl steht an der Achse.

    ``adjusted()`` liefert den Zehnerexponenten der ersten geltenden Stelle;
    daraus wird die Stufe, auf die aufgerundet wird. Aufwärts und nie abwärts:
    ein abgerundeter Deckel schnitte Perioden ab, die vorher hineinpassten.
    """
    if wert <= 0:
        return Decimal("0")
    stufe = Decimal(10) ** (wert.adjusted() - 1)
    return (wert / stufe).to_integral_value(rounding=ROUND_CEILING) * stufe


def deckel(werte: list[Decimal]) -> Decimal:
    """Obergrenze der Achse: der Höchstwert — oder ein robuster Wert darunter.

    Ein einmalig gekauftes Gerät kostet ein Vielfaches einer Monatsrechnung.
    Richtet sich die Achse danach, sind die laufenden Monate untereinander nicht
    mehr zu unterscheiden, und genau deren Vergleich ist der Zweck des Bildes.

    Der Deckel ist darum das grössere von 90. Perzentil und Median mal
    ``DECKEL_FAKTOR`` — das erste trägt eine Reihe, die auf ein neues Niveau
    springt, das zweite eine, die um ihren Normalwert schwankt. Er greift nur,
    wenn der Höchstwert ihn um ``DECKEL_SCHWELLE`` überragt: sonst bleibt die
    Achse beim echten Höchstwert, und es wird nichts abgeschnitten.

    Das Perzentil ist ein Wert der Reihe und nicht interpoliert — der Deckel soll
    eine Grösse sein, die es wirklich gibt. Weggelassen wird das oberste Zehntel,
    mindestens aber ein Wert: bei zwölf Perioden ist genau der eine Gerätekauf
    das oberste Zehntel.
    """
    reihe = sorted(w for w in werte if w > 0)
    if not reihe:
        return Decimal("0")
    hoechst = reihe[-1]
    if len(reihe) < MIN_PERIODEN_DECKEL:
        return hoechst
    median = reihe[(len(reihe) - 1) // 2]
    perzentil = reihe[-1 - max(1, len(reihe) // 10)]
    kandidat = max(perzentil, median * DECKEL_FAKTOR)
    if hoechst <= kandidat * DECKEL_SCHWELLE:
        return hoechst
    return _aufrunden(kandidat)


# Wie viele leere Perioden hoechstens angehaengt werden. Eine Reihe, die seit
# Jahren stillsteht, soll die Luecke ZEIGEN und nicht das Bild fuellen: bei
# monatlichem Takt sind zwoelf Spalten schon ein Jahr Schweigen, und mehr
# Spalten machten die vorhandenen nur duenner.
MAX_LEERE = 12

# Mehr Achsenmarken passen am Handy nicht nebeneinander (dieselbe Zahl wie in
# ``services/zeitachse.py``; dort steht die Begruendung).
MAX_XMARKEN = 5

# Monate je Takt. ``UNREGELMAESSIG`` fehlt bewusst: dort gibt es keine erwartete
# naechste Periode, und eine leere Spalte behauptete eine Luecke, die niemand
# zugesagt hat.
_TAKT_MONATE = {
    MetricCadence.MONATLICH: 1,
    MetricCadence.QUARTALSWEISE: 3,
    MetricCadence.JAEHRLICH: 12,
}


@dataclass(frozen=True)
class _LeerPunkt:
    """Platzhalter mit Periode und ohne Wert.

    Eigene Klasse statt eines ``Punkt`` mit Wert null: null ist ein erfasster
    Betrag, „nicht erfasst" ist keiner. Die beiden zu verwechseln ist genau der
    Fehler, den dieses Diagramm nicht machen darf.
    """

    start: date
    ende: date


def _leere_perioden(v: Verlauf, roh: list[dict], heute: date) -> list[dict]:
    """Perioden zwischen dem letzten Wert und heute — ohne Betrag.

    Ohne sie endete die Achse am letzten Wert, und eine Reihe, deren juengster
    Eintrag ein Jahr alt ist, sah genauso aktuell aus wie eine von gestern. Die
    leeren Spalten SIND die Aussage.
    """
    schritt = _TAKT_MONATE.get(v.reihe.cadence)
    if not roh or schritt is None:
        return []
    leer: list[dict] = []
    start = add_months(roh[-1]["punkt"].start, schritt)
    while start <= heute and len(leer) < MAX_LEERE:
        leer.append({
            "punkt": _LeerPunkt(start=start, ende=add_months(start, schritt) - timedelta(days=1)),
            "leer": True,
        })
        start = add_months(start, schritt)
    return leer


def _pfad(balken: list[Balken]) -> str:
    """Die Bezahlt-Linie — an leeren Perioden unterbrochen.

    Ein durchgezogener Zug haette die Linie auf den Boden gezogen (dort sitzt
    ein Balken ohne Wert) und damit „null bezahlt" behauptet. Nach einer Luecke
    beginnt darum ein neuer Teilzug mit ``M``.
    """
    teile: list[str] = []
    neu = True
    for b in balken:
        if b.leer:
            neu = True
            continue
        teile.append(f"{'M' if neu else 'L'} {b.x} {b.y}")
        neu = False
    return " ".join(teile)


def _segmente(
    posten: list[tuple[str, str, Decimal, bool]], decke: Decimal,
) -> tuple[list[Segment], list[tuple[str, Decimal]]]:
    """Bänder eines Stapels bis zur Achsendecke — und was darüber liegt.

    ``anteil`` ist der Anteil am GEZEICHNETEN Stapel. Nur so bleiben die Bänder
    untereinander proportional zu ihren Beträgen und behalten zugleich dieselben
    Pixel je Franken wie die Bänder der übrigen Perioden: der Stapel wird nicht
    gestaucht, sondern an der Decke abgeschnitten.
    """
    summe = sum((b for _, _, b, _ in posten), Decimal("0"))
    gezeichnet = min(summe, decke)
    if gezeichnet <= 0:
        return [], [(name, betrag) for name, _, betrag, _ in posten]
    segmente: list[Segment] = []
    ueber: list[tuple[str, Decimal]] = []
    belegt = Decimal("0")
    for name, farbe, betrag, rabatt in posten:
        sichtbar = min(betrag, max(decke - belegt, Decimal("0")))
        belegt += betrag
        if sichtbar > 0:
            segmente.append(Segment(
                name=name, farbe=farbe, betrag=betrag,
                anteil=round(float(sichtbar / gezeichnet) * 100, 2), rabatt=rabatt,
            ))
        if sichtbar < betrag:
            ueber.append((name, betrag))
    return segmente, ueber


def _text(wert: Decimal, einheit: MetricUnit, *, minus: bool = False) -> str:
    """Betrag für Aufklapper und Werte-Kasten.

    Rabatte tragen ein echtes Minus (U+2212, wie ``chf_wert``): der Bindestrich
    der Tastatur ist im Fliesstext ein Trennstrich und wird bei kleiner Schrift
    übersehen — gerade dort, wo das Vorzeichen die ganze Aussage trägt.
    """
    text = formatiere(wert, einheit)
    return f"−{text}" if minus else text


def positions_bild(v: Verlauf, heute: date | None = None) -> Bild | None:
    """Balkenbild einer Reihe — oder ``None``, wenn sie keine Positionen trägt.

    ``None`` heisst für das Template: bei der Linie bleiben. Reihen ohne
    Aufschlüsselung (Prämie, Lohn, Vorsorge) sollen sich nicht ändern, nur weil
    eine andere Reihe jetzt Balken kann.
    """
    if not v.punkte or not hat_positionen(v):
        return None
    # Vorgabe statt Pflichtargument: die Aufrufer in der App reichen den Tag
    # durch, die Tests setzen ihn fest. Ein Pflichtargument haette fuenfzig
    # Testaufrufe zu Aenderungen gezwungen, die nichts pruefen.
    heute = heute or heute_lokal()

    einheit = v.reihe.unit
    je_punkt = [(p, positionen(p)) for p in v.punkte]

    gewicht: dict[str, Decimal] = {}
    spitze: dict[str, Decimal] = {}
    positiv: set[str] = set()
    negativ: set[str] = set()
    for _, pos in je_punkt:
        for name, betrag in pos.items():
            gewicht[name] = gewicht.get(name, Decimal("0")) + abs(betrag)
            spitze[name] = max(spitze.get(name, Decimal("0")), abs(betrag))
            if betrag > 0:
                positiv.add(name)
            elif betrag < 0:
                negativ.add(name)
    farbe_von = farbzuordnung(gewicht)
    rest_namen = [n for n in sorted(gewicht) if n not in farbe_von]

    def _nur_rabatt(namen: set[str]) -> bool:
        return bool(namen & negativ) and not (namen & positiv)

    # STAPELREIHENFOLGE, von unten nach oben: nach dem grössten EINZELBETRAG, den
    # eine Position in einer Periode erreicht — der Sammelposten zuunterst. Nicht
    # alphabetisch und nicht nach Gesamtgewicht, und zwar wegen der Achsendecke:
    # abgeschnitten wird OBEN. Lag dort ein laufender Posten, verschwand er im
    # Ausreissermonat aus dem Bild, während der einmalige Gerätekauf als einziges
    # Band stehen blieb — gemessen bestand der Balken zu 100 % aus dem Gerät.
    # Umgekehrt bleibt der Monat mit seinen Nachbarn vergleichbar: unten dasselbe
    # Bild wie immer, oben das gekappte Band. Die Reihenfolge ist eine Grösse der
    # ganzen Reihe und springt darum nicht von Periode zu Periode.
    stapel_namen = sorted(farbe_von, key=lambda n: (spitze[n], n))

    # Reihenfolge des Werte-Kastens: das Grösste zuerst, erst was berechnet wird,
    # dann was abgezogen wird, zuletzt die Rundung — buchhalterisch untereinander,
    # damit sich der bezahlte Betrag von oben nach unten nachrechnen lässt. Die
    # LEGENDE folgt dem Stapel: sie erklärt Bänder, der Kasten rechnet vor.
    kasten_alle = list(reversed(stapel_namen)) + rest_namen
    kasten_namen = [n for n in kasten_alle if not _nur_rabatt({n})]
    kasten_namen += [n for n in kasten_alle if _nur_rabatt({n})]
    schluessel_von = {n: str(i) for i, n in enumerate(kasten_namen)}
    hat_rundung = any(rundung(p) for p, _ in je_punkt)
    rundung_schluessel = str(len(kasten_namen))

    roh: list[dict] = []
    for punkt, pos in je_punkt:
        oben: list[tuple[str, str, Decimal, bool]] = []
        unten: list[tuple[str, str, Decimal, bool]] = []
        rest_oben = rest_unten = Decimal("0")

        # Der Sammelposten zuunterst: seine Mitglieder sind die kleinsten der
        # Reihe, und an der Decke abgeschnitten sagte ein Band namens „Übrige"
        # nichts darüber, was dort fehlt.
        for name in rest_namen:
            betrag = pos.get(name)
            if not betrag:
                continue
            if betrag < 0:
                rest_unten += -betrag
            else:
                rest_oben += betrag
        if rest_oben:
            oben.append((REST_NAME, REST_FARBE, rest_oben, False))
        if rest_unten:
            unten.append((REST_NAME, REST_FARBE, rest_unten, True))
        # Dann die farbtragenden Posten in fester Stapelreihenfolge. Springt ein
        # Band von Periode zu Periode an eine andere Stelle, ist der Verlauf
        # einer Position mit dem Auge nicht mehr zu verfolgen. Ein Betrag von
        # null bekommt KEIN Band — eine Höhe von null lässt sich nicht zeichnen;
        # seine Zeile im Kasten steht trotzdem (siehe unten).
        for name in stapel_namen:
            betrag = pos.get(name)
            if not betrag:
                continue
            (unten if betrag < 0 else oben).append(
                (name, farbe_von[name], abs(betrag), betrag < 0))

        # Die Zeile steht auch bei 0.00: „Position mit null auf der Rechnung" ist
        # etwas anderes als „Position kommt nicht vor", und nur die Zeile kann
        # das sagen.
        zeilen = [
            Zeile(schluessel_von[n], n, _text(abs(pos[n]), einheit, minus=pos[n] < 0))
            for n in kasten_namen if n in pos
        ]
        diff = rundung(punkt)
        if diff:
            zeilen.append(Zeile(rundung_schluessel, RUNDUNG_NAME,
                                _text(abs(diff), einheit, minus=diff < 0)))
        # Auch der bezahlte Betrag mit echtem Minus: eine Gutschrift stünde sonst
        # mit dem Bindestrich der Tastatur unter lauter U+2212-Rabatten.
        zeilen.append(Zeile(BEZAHLT, "bezahlt",
                            _text(abs(punkt.wert), einheit, minus=punkt.wert < 0)))

        brutto = sum((b for _, _, b, _ in oben), Decimal("0"))
        rabatt = sum((b for _, _, b, _ in unten), Decimal("0"))
        offen = not pos
        if offen:
            # Ohne Aufschlüsselung ist der bezahlte Betrag die einzige bekannte
            # Grösse. Er trägt den offenen Balken, sonst stünde die Periode als
            # Null da — und eine Null ist etwas anderes als „nicht erfasst".
            brutto = punkt.wert if punkt.wert > 0 else Decimal("0")

        roh.append({"punkt": punkt, "oben": oben, "unten": unten, "brutto": brutto,
                    "rabatt": rabatt, "offen": offen, "zeilen": zeilen})

    # Die Bezahlt-Linie geht in die obere Achse ein: bei einer ausgewiesenen
    # Rundung nach oben liegt sie über dem Brutto-Balken, und eine Achse, die nur
    # die Balken kennt, schnitte sie am oberen Rand ab.
    decke = deckel([max(b["brutto"], b["punkt"].wert) for b in roh])
    decke_unten = deckel([b["rabatt"] for b in roh])
    if decke > 0:
        # Ein Rabatt reicht nie TIEFER, als die Achse hoch ist. Ohne diese
        # Schranke nahm die Rabattzeile für eine einzige Rückgabe-Gutschrift mehr
        # als die halbe Zeichenfläche (gemessen 57 %) — und zwar genau dann, wenn
        # nur eine Periode überhaupt einen Rabatt trägt und es deshalb keinen
        # laufenden Wert gibt, gegen den sie ein Ausreisser wäre. Der zu tiefe
        # Balken wird stattdessen wie oben gekappt und benannt.
        decke_unten = min(decke_unten, decke)

    # EINE Skala für oben und unten: die Zeilenhöhen verhalten sich wie die
    # beiden Achsen, damit ein Millimeter oben denselben Betrag bedeutet wie
    # einer unten. ``MIN_UNTEN``/``MIN_OBEN`` geben einer Zeile nur LUFT — die
    # Balken darin behalten die Pixel je Franken der anderen Zeile, sonst wäre
    # die gemeinsame Skala genau dort aufgegeben, wo sie am meisten zählt.
    gesamt = decke + decke_unten
    unten = float(decke_unten / gesamt) if gesamt > 0 else 0.0
    if decke_unten > 0:
        unten = max(unten, MIN_UNTEN)
    # Auch ohne Balken über der Nulllinie braucht die obere Zeile Höhe: dort
    # steht die „0", und die Nulllinie selbst ist der Bezugspunkt des Bildes.
    unten = min(unten, 1.0 - MIN_OBEN)
    oben = 1.0 - unten
    anteil_oben = round(oben * 100, 2)

    # Anteil der Gesamthöhe je Franken. Das Minimum der beiden Zeilen: die Zeile,
    # die Luft bekommen hat, hätte für sich die grössere Skala — und genau die
    # darf nicht gelten, sonst zeichnete sie mehr Pixel je Franken als die andere.
    massstaebe = [oben / float(decke)] if decke > 0 else []
    if decke_unten > 0:
        massstaebe.append(unten / float(decke_unten))
    skala = min(massstaebe) if massstaebe else 0.0

    def _hoch(betrag: Decimal) -> float:
        """Höhe in Prozent der OBEREN Zeile, an der Decke abgeschnitten."""
        return round(min(float(betrag) * skala / oben, 1.0) * 100, 2)

    def _tief(betrag: Decimal) -> float:
        """Tiefe in Prozent der UNTEREN Zeile, an der Decke abgeschnitten."""
        if unten <= 0:
            return 0.0
        return round(min(float(betrag) * skala / unten, 1.0) * 100, 2)

    roh = roh + _leere_perioden(v, roh, heute)
    anzahl = len(roh)
    balken: list[Balken] = []
    kappungen: list[Kappung] = []
    for i, b in enumerate(roh):
        punkt = b["punkt"]
        label = periode_text(v.reihe.cadence, punkt.start, punkt.ende)
        if b.get("leer"):
            balken.append(Balken(
                punkt_id=-(i + 1), label=label,
                brutto=Decimal("0"), rabatt=Decimal("0"), bezahlt=Decimal("0"),
                oben=[], unten=[], h_oben=0.0, h_unten=0.0,
                x=round((i + 0.5) / anzahl * 100, 2), y=100.0,
                offen=False, unsicher=False, zeilen=[], leer=True,
            ))
            continue
        oben_seg, ueber_oben = _segmente(b["oben"], decke)
        unten_seg, ueber_unten = _segmente(b["unten"], decke_unten)
        # Ungeklemmt gerechnet, damit „liegt ausserhalb" überhaupt auffällt: ein
        # negativ bezahlter Monat schob den Punkt sonst unter das Zeichenfeld,
        # und die Linie brach dort ohne Angabe eines Grundes ab.
        y_roh = 100 - float(punkt.wert) * skala / oben * 100
        y = min(100.0, max(0.0, y_roh))
        balken.append(Balken(
            punkt_id=punkt.id,
            label=label,
            brutto=b["brutto"],
            rabatt=b["rabatt"],
            bezahlt=punkt.wert,
            oben=oben_seg,
            unten=unten_seg,
            h_oben=_hoch(b["brutto"]),
            h_unten=_tief(b["rabatt"]),
            # Lückenlose Spalten → die Mitte der i-ten Spalte liegt exakt bei
            # (i+0.5)/n. Mit einem Spalt zwischen den Spalten wanderten Balken
            # und Linienpunkt auseinander, am stärksten an den Rändern.
            x=round((i + 0.5) / anzahl * 100, 2),
            y=round(y, 2),
            offen=b["offen"],
            unsicher=bool(punkt.extras.get(_UNSICHER)),
            zeilen=b["zeilen"],
            # Auch der offene Balken (Höhe bekannt, Zusammensetzung nicht) wird
            # gekappt — er hat keine Bänder, die dabei wegfielen, und ohne Marke
            # behauptete seine Kante trotzdem eine Höhe, die er nicht hat.
            gekappt=b["brutto"] > decke,
            gekappt_unten=b["rabatt"] > decke_unten,
            y_aus=abs(y_roh - y) > 0.01,
        ))
        bezahlt_aus = (
            _text(abs(punkt.wert), einheit, minus=punkt.wert < 0)
            if abs(y_roh - y) > 0.01 else None
        )
        if ueber_oben or ueber_unten or bezahlt_aus:
            kappungen.append(Kappung(
                label=label,
                posten=[(n, _text(w, einheit)) for n, w in ueber_oben],
                posten_unten=[(n, _text(w, einheit, minus=True)) for n, w in ueber_unten],
                bezahlt=bezahlt_aus,
            ))

    # Legende in Stapelreihenfolge, das erste Muster ist das unterste Band.
    posten = [Posten(name=REST_NAME, farbe=REST_FARBE,
                     nur_rabatt=_nur_rabatt(set(rest_namen)))] if rest_namen else []
    posten += [
        Posten(name=n, farbe=farbe_von[n], nur_rabatt=_nur_rabatt({n}))
        for n in stapel_namen
    ]
    vorlage = [
        Posten(name=n, farbe=farbe_von.get(n, REST_FARBE), nur_rabatt=_nur_rabatt({n}),
               schluessel=schluessel_von[n])
        for n in kasten_namen
    ]
    # Die Rundung steht NUR im Kasten, nicht in der Legende: sie hat kein Band im
    # Balken, und eine Legende erklärt Bänder. Ein Farbmuster für etwas, das im
    # Bild nicht vorkommt, schickt die Suche danach ins Leere.
    if hat_rundung:
        vorlage.append(Posten(name=RUNDUNG_NAME, farbe=REST_FARBE, nur_rabatt=False,
                              schluessel=rundung_schluessel))

    return Bild(
        balken=balken,
        posten=posten,
        zeilen_vorlage=vorlage,
        deckel=decke,
        deckel_unten=decke_unten,
        anteil_oben=anteil_oben,
        marke_unten=_tief(decke_unten),
        kappungen=kappungen,
        pfad=_pfad(balken),
        zeigt_oben=decke > 0,
        zeigt_rabatt=decke_unten > 0,
        hat_offene=any(b.offen for b in balken),
        hat_unsichere=any(b.unsicher for b in balken),
    )


def bilder(verlaeufe: list[Verlauf], heute: date | None = None) -> dict[str, Bild]:
    """slug → Balkenbild, nur für Reihen, die Positionen tragen."""
    return {
        v.reihe.slug: bild
        for v in verlaeufe
        if (bild := positions_bild(v, heute)) is not None
    }
