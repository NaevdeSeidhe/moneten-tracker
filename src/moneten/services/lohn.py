"""Die Zusammensetzung einer Lohn-Gutschrift: Bruttolohn, Abzüge, Nettolohn.

**Das Problem, um das sich alles hier dreht.** Es gibt keine monatlichen
Lohnabrechnungen. Vorhanden sind Jahreswerte (Lohnausweis: Bruttolohn, Abzüge,
Nettolohn eines ganzen Jahres; Vorsorgeausweis: Beiträge pro Jahr und pro Monat)
und ein Arbeitsvertrag. Daraus lässt sich ein einzelner Monat nur **schätzen** —
ein Monat mit Bonus, Nachzahlung oder Pensumsänderung stimmt nicht, und man
sieht es der Zahl nicht an.

Dieses Modul löst das an drei Stellen, und alle drei sind Absicht:

1. **Jeder Posten trägt seine Herkunft** (:class:`~moneten.db.models.LohnHerkunft`)
   in drei Stufen: erfasst (für diesen Monat eingetragen), fortgeschrieben
   (unverändert aus einem früheren, erfassten Monat) und gerechnet (aus einem
   Jahreswert oder Beitragssatz abgeleitet). Die Anzeige kennzeichnet die
   beiden unteren Stufen mit je einem eigenen Zeichen (:data:`MARKE`).
2. **Der Nettolohn wird nie gespeichert.** Er ergibt sich aus den Posten. Ein
   gespeicherter Nettolohn liesse sich (versehentlich oder bequem) an den
   gebuchten Betrag angleichen — und eine geschätzte Aufstellung sähe exakt aus.
3. **Die Differenz zum gebuchten Betrag wird gezeigt, nicht verrechnet.** Sie
   ist das Mass dafür, wie gut die Schätzung trifft. Wer sie versteckt,
   verspricht eine Genauigkeit, die die Datenlage nicht hergibt.

Eine erfundene Genauigkeit ist schlimmer als eine Lücke — deshalb füllt dieses
Modul auch keine Lücke, für die es keine Quelle hat (siehe :func:`vorschlag`:
NBUV, KTG und Pensionskasse bleiben leer, wenn nichts erfasst ist). Die Quelle
für diese drei ist der Nutzer selbst: erfasst er EINEN Monat von Hand, speist
sich jeder weitere Vorschlag daraus.

Die vierte Stelle ist die Gegenrechnung über ein ganzes Jahr
(:func:`jahresprobe`) — sie ist der einzige Ort im Modul, an dem eine
fortgeschriebene Zahl überhaupt widerlegt werden kann.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from moneten.dates import heute_lokal
from moneten.db.models import (
    Lohnabrechnung,
    LohnHerkunft,
    Lohnposten,
    LohnPostenArt,
    ManagementType,
    MetricPoint,
    MetricSeries,
    Transaction,
)
from moneten.templating import MONATE, chf_wert

# Gesetzliche Arbeitnehmer-Anteile der Sozialversicherung, in Prozent des
# Bruttolohns. Sie stehen im Gesetz und nicht in den Unterlagen — anders als
# die Sätze für NBUV, KTG und den Pensionskassenplan, die vom Arbeitgeber
# abhängen und darum bewusst KEINEN Vorgabewert haben (:func:`vorschlag`).
# Ein Vorgabewert stünde im Code und wäre für jeden Arbeitgeber falsch;
# erfasst der Nutzer sie einmal, stehen sie in seinen Daten und stimmen.
#
# Sie erzeugen nur Vorschläge, nie gespeicherte Wahrheiten: jeder daraus
# gerechnete Posten trägt ``GERECHNET``, bis der Nutzer ihn überschreibt.
SAETZE: tuple[tuple[str, Decimal], ...] = (
    ("AHV/IV/EO", Decimal("5.3")),
    ("ALV", Decimal("1.1")),
)

# Abzüge, für die es keinen ableitbaren Wert gibt — als leere Zeile im Editor,
# damit klar ist, dass sie fehlen, statt sie stillschweigend wegzulassen.
#
# Die Bezeichnungen sind die Kürzel, die auf einer Schweizer Lohnabrechnung
# stehen (NBUV = Nichtberufsunfall, KTG = Krankentaggeld). Fehlt eine Zeile in
# dieser Liste, legt der Nutzer sie in jedem Monat neu von Hand an — und
# benennt sie jedes Mal etwas anders. Verschiedene Namen für denselben Abzug
# machen die Monate untereinander unvergleichbar.
OHNE_QUELLE: tuple[str, ...] = ("NBUV", "KTG", "Pensionskasse")

# Dasselbe auf der Brutto-Seite. Der Anteil am 13. Monatslohn wird anteilig mit
# jeder Monatszahlung ausgerichtet und steht als eigene Position auf dem
# Lohnblatt — er ist also kein Sonderfall des Dezembers, sondern eine Zeile, die
# jeder Monat hat. Wie gross dieser Anteil ist, steht im Arbeitsvertrag und
# nicht hier: ein Vorgabewert wäre geraten. Ohne die Zeile legt der Nutzer sie
# jeden Monat neu an und benennt sie jedes Mal anders — dieselbe Falle wie bei
# NBUV und KTG, nur auf der anderen Seite der Aufstellung.
BRUTTO_OHNE_QUELLE: tuple[str, ...] = ("Anteil 13. Monatslohn",)

# Reihe, aus der ein Monatsbrutto geschätzt wird (Lohnausweis-Jahreswert), und
# die Reihe mit dem Pensionskassen-Monatsbeitrag aus dem Vorsorgeausweis.
REIHE_JAHRESLOHN = "lohn"
REIHE_VORSORGE = "pk_guthaben"
NEBENWERT_PK_MONAT = "beitrag_monat"

# Ein Jahreswert wird durch 12 geteilt und nicht durch 13, und zwar NICHT
# deshalb, weil es keinen 13. Monatslohn gäbe: der Anteil daran wird anteilig
# mit jeder Monatszahlung ausgerichtet und steht als eigene Position auf dem
# Lohnblatt. Er steckt damit bereits in jedem der zwölf Beträge — eine
# dreizehnte Auszahlung, auf die sich der Jahreswert verteilen liesse, gibt es
# nicht. Durch 13 zu teilen ergäbe zwölf zu kleine Monate.
MONATE_IM_JAHR = Decimal("12")

_RAPPEN = Decimal("0.01")

# Zeichen je Herkunft — die ganze Kennzeichnung der Anzeige steht hier und
# nirgends sonst. Sie erscheint an drei Stellen (Aufklapper an der Buchung,
# mitlaufende Gegenprobe, Marke neben jedem Eingabefeld); dreimal geschrieben
# liefen die drei auseinander, und die Legende erklärte ein Zeichen, das
# woanders anders aussah.
#
# Die Wahl ist nicht beliebig: „≈" heisst ungefähr, „=" heisst gleich. Genau das
# ist der Unterschied zwischen den beiden Stufen — eine gerechnete Zahl ist eine
# Näherung, eine fortgeschriebene ist der EXAKTE Wert eines früheren Blattes und
# höchstens veraltet. Erfasst trägt kein Zeichen: eine Zahl ohne Marke ist eine,
# die auf dem Blatt dieses Monats steht.
MARKE: dict[LohnHerkunft, str] = {
    LohnHerkunft.ERFASST: "",
    LohnHerkunft.FORTGESCHRIEBEN: "=",
    LohnHerkunft.GERECHNET: "≈",
}

# Was die beiden Zeichen bedeuten — je Stufe EIN Halbsatz, OHNE das Zeichen. Er
# steht einmal je Aufstellung im Fuss (davor dann das Zeichen) und ein zweites
# Mal als `title` der Marke neben dem Eingabefeld, wo das Zeichen schon dasteht.
# Stünde es im Text, läse der Tooltip „≈ ≈ gerechnet".
LEGENDE: dict[LohnHerkunft, str] = {
    LohnHerkunft.FORTGESCHRIEBEN: "unverändert aus einem früheren Monat",
    LohnHerkunft.GERECHNET: "gerechnet, nicht abgelesen",
}

# Von der besten Kenntnis zur schlechtesten. Reihenfolge zählt: eine Summe erbt
# die SCHWÄCHSTE Stufe ihrer Summanden (:func:`_schwaechste`).
_STUFEN: tuple[LohnHerkunft, ...] = (
    LohnHerkunft.ERFASST,
    LohnHerkunft.FORTGESCHRIEBEN,
    LohnHerkunft.GERECHNET,
)


def _schwaechste(herkuenfte: list[LohnHerkunft]) -> LohnHerkunft:
    """Die schwächste der übergebenen Stufen; eine leere Liste gilt als erfasst.

    Eine Summe ist nie genauer als ihr unsicherster Summand. Ein Bruttolohn aus
    einem abgelesenen Monatslohn plus einer gerechneten Zulage ist gerechnet,
    nicht abgelesen — sonst behauptete ausgerechnet die Zeile, auf die man
    schaut, mehr Genauigkeit als jede Zeile darunter.
    """
    return max(herkuenfte, key=_STUFEN.index, default=LohnHerkunft.ERFASST)


def _runden(wert: Decimal) -> Decimal:
    """Auf Rappen, kaufmaennisch aufgerundet.

    ``ROUND_HALF_UP`` ausdruecklich statt der Decimal-Vorgabe
    ``ROUND_HALF_EVEN``: Abos, Median-Budget und die Jahresposten runden im
    ganzen Projekt so. Ohne die Angabe wich der Lohnblock als einziger ab —
    immer dann, wenn ein Prozentsatz auf einem halben Rappen landet. Die
    Vorgabe rundet dort zur geraden Ziffer und liegt damit einen Rappen neben
    dem Rest der App.
    """
    return wert.quantize(_RAPPEN, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Zeile:
    """Ein Posten, fertig für die Anzeige."""

    label: str
    betrag: Decimal
    herkunft: LohnHerkunft

    @property
    def marke(self) -> str:
        """Das Zeichen vor dem Betrag — leer bei einer abgelesenen Zahl."""
        return MARKE[self.herkunft]


@dataclass(frozen=True)
class Aufstellung:
    """Eine Lohn-Gutschrift, aufgeschlüsselt und dem gebuchten Betrag gegenübergestellt."""

    brutto_zeilen: list[Zeile]
    abzug_zeilen: list[Zeile]
    brutto: Decimal
    abzuege: Decimal
    #: Nettolohn aus den Posten — nicht gespeichert, immer neu gerechnet.
    netto: Decimal
    #: Was wirklich auf dem Konto ankam (der Betrag der Buchung).
    gebucht: Decimal
    grundlage: str | None

    @property
    def differenz(self) -> Decimal:
        """Gerechneter Nettolohn minus gebuchter Betrag.

        Nicht null zu sein ist der Normalfall, sobald ein Posten gerechnet ist —
        deshalb wird sie gezeigt und nicht wegdefiniert.
        """
        return _runden(self.netto - self.gebucht)

    @property
    def alle_zeilen(self) -> list[Zeile]:
        return [*self.brutto_zeilen, *self.abzug_zeilen]

    @property
    def geschaetzt(self) -> bool:
        """Steckt mindestens ein Betrag darin, der nicht vom Blatt DIESES Monats stammt?

        Solange das zutrifft, steht die ganze Aufstellung unter Vorbehalt — auch
        wenn die Hälfte der Zeilen abgelesen ist. Eine Summe ist nie genauer als
        ihr unsicherster Summand.
        """
        return self.netto_marke != ""

    @property
    def netto_marke(self) -> str:
        """Zeichen der Ergebniszeile: die schwächste Stufe der ganzen Aufstellung."""
        return MARKE[_schwaechste([z.herkunft for z in self.alle_zeilen])]

    @property
    def brutto_marke(self) -> str:
        """Zeichen der Summenzeile „Bruttolohn"."""
        return MARKE[_schwaechste([z.herkunft for z in self.brutto_zeilen])]

    @property
    def abzuege_marke(self) -> str:
        """Zeichen der Summenzeile „Abzüge"."""
        return MARKE[_schwaechste([z.herkunft for z in self.abzug_zeilen])]

    @property
    def legende(self) -> list[str]:
        """Zeichen und Erklärung je Stufe, die in dieser Aufstellung vorkommt.

        Der Fuss nennt jede Stufe GENAU EINMAL. Die Erklärung an jede Zeile zu
        hängen hiesse, denselben Halbsatz bis zu acht Mal untereinander zu
        drucken — und der Block ist auf 375px ohnehin das längste Element der
        Buchungsliste. Eine Stufe, die hier nicht vorkommt, wird auch nicht
        erklärt: eine Legende für ein Zeichen, das nirgends steht, ist Lärm.
        """
        vorhanden = {z.herkunft for z in self.alle_zeilen}
        return [
            f"{MARKE[stufe]} {LEGENDE[stufe]}"
            for stufe in _STUFEN
            if stufe in vorhanden and stufe in LEGENDE
        ]

    @property
    def differenz_ist_fehler(self) -> bool:
        """Eine Abweichung, die niemand erklären kann.

        Bei geschätzten Posten ist eine Differenz der Normalfall und nichts
        weiter. Sind dagegen ALLE Beträge erfasst, behauptet die Aufstellung,
        abgelesen zu sein — dann ist eine Abweichung ein Tippfehler oder ein
        vergessener Abzug und gehört hervorgehoben.
        """
        return not self.geschaetzt and self.differenz != 0


def aufstellung_aus_posten(
    posten: list[tuple[LohnPostenArt, str, Decimal, LohnHerkunft]],
    gebucht: Decimal,
    *,
    grundlage: str | None,
) -> Aufstellung:
    """Baut die Aufstellung aus Posten, die noch nirgends gespeichert sein müssen.

    Diese Trennung ist der Grund, warum die mitlaufende Gegenprobe im Editor
    dasselbe zeigt wie der Aufklapper an der Buchung: beide gehen durch diese
    Funktion. Rechnete der Editor selbst — in einer zweiten Sprache, im Browser —
    liefe die eine Darstellung der anderen davon, und zwar genau an der Stelle,
    an der der Nutzer entscheidet.
    """
    brutto_zeilen = [
        Zeile(label, betrag, herkunft)
        for art, label, betrag, herkunft in posten
        if art == LohnPostenArt.BRUTTO
    ]
    abzug_zeilen = [
        Zeile(label, betrag, herkunft)
        for art, label, betrag, herkunft in posten
        if art != LohnPostenArt.BRUTTO
    ]
    brutto = _runden(sum((z.betrag for z in brutto_zeilen), Decimal("0")))
    abzuege = _runden(sum((z.betrag for z in abzug_zeilen), Decimal("0")))
    return Aufstellung(
        brutto_zeilen=brutto_zeilen,
        abzug_zeilen=abzug_zeilen,
        brutto=brutto,
        abzuege=abzuege,
        netto=_runden(brutto - abzuege),
        gebucht=gebucht,
        grundlage=grundlage,
    )


def aufstellung(abrechnung: Lohnabrechnung, tx: Transaction) -> Aufstellung:
    """Rechnet die Posten einer Buchung zur Anzeige-Aufstellung zusammen.

    Verglichen wird mit ``tx.amount`` — dem Betrag der Buchung selbst, auch wenn
    sie in Kategorie-Anteile aufgeteilt ist (:class:`TransactionSplit`). Eine
    Aufteilung verschiebt kein Geld, sondern nur die Zuordnung; die Summe der
    Anteile ist derselbe Betrag und wäre als Vergleichsgrösse nur ein Umweg.
    """
    return aufstellung_aus_posten(
        [(p.art, p.label, p.betrag, p.herkunft) for p in abrechnung.posten],
        tx.amount,
        grundlage=abrechnung.grundlage,
    )


def darf_aufschluesseln(tx: Transaction) -> bool:
    """Kommt für diese Buchung eine Lohnzusammensetzung überhaupt in Frage?

    Nettolohn ist eine **Einnahme** — der Betrag ist echt positiv. Eine Buchung
    über 0.00 ist keine Lohnzahlung; sie bekam den Editor bisher angeboten, weil
    die Grenze bei ``>= 0`` lag. Eine Umbuchung zwischen eigenen Konten ist
    ebenfalls keine Einnahme, auch wenn ihre Gutschriftsseite positiv aussieht.
    """
    return tx.amount > 0 and tx.management_type != ManagementType.TRANSFER


def abrechnung_zu(db: Session, tx_id: int) -> Lohnabrechnung | None:
    """Die Aufstellung einer Buchung, falls erfasst."""
    return db.scalar(
        select(Lohnabrechnung)
        .where(Lohnabrechnung.transaction_id == tx_id)
        .options(selectinload(Lohnabrechnung.posten))
    )


def aufstellungen_zu(db: Session, txs: list[Transaction]) -> dict[int, Aufstellung]:
    """Aufstellungen der sichtbaren Buchungen — eine Abfrage statt einer je Zeile."""
    ids = [t.id for t in txs]
    if not ids:
        return {}
    nach_id = {t.id: t for t in txs}
    stmt = (
        select(Lohnabrechnung)
        .where(Lohnabrechnung.transaction_id.in_(ids))
        .options(selectinload(Lohnabrechnung.posten))
    )
    return {
        a.transaction_id: aufstellung(a, nach_id[a.transaction_id])
        for a in db.scalars(stmt)
    }


# ---------------------------------------------------------------------------
# Vorschlag: was sich aus vorhandenen Unterlagen ableiten lässt
# ---------------------------------------------------------------------------


def _zeile(
    label: str, art: LohnPostenArt, betrag: Decimal | None, herkunft: LohnHerkunft
) -> dict:
    """Eine Editor-Zeile. Leerer Betrag heisst: es gibt dafür keine Quelle."""
    return {
        "label": label,
        "art": art.value,
        "betrag": f"{betrag:.2f}" if betrag is not None else "",
        "herkunft": herkunft.value,
    }


def _fortgeschrieben(herkunft: LohnHerkunft) -> LohnHerkunft:
    """Wie ein übernommener Posten in DIESEM Monat zu führen ist.

    Die Trennlinie ist, ob der Wert auf ein Blatt zurückgeht. Ein abgelesener
    Posten (auch ein bereits fortgeschriebener, der auf ein Blatt zurückführt)
    wird fortgeschrieben: exakt, nur aus einem anderen Monat. Ein gerechneter
    bleibt gerechnet — Abschreiben macht aus einer Schätzung keine Ablesung.
    """
    return (
        LohnHerkunft.GERECHNET
        if herkunft == LohnHerkunft.GERECHNET
        else LohnHerkunft.FORTGESCHRIEBEN
    )


def _letzte_abrechnung(db: Session, tx: Transaction) -> tuple[Lohnabrechnung, date] | None:
    """Die zuletzt erfasste Aufstellung einer ANDEREN Lohnbuchung.

    Bevorzugt wird ein Monat, in dem MINDESTENS EIN Betrag vom Nutzer selbst
    stammt (``ERFASST``). Ohne diese Vorauswahl hängte sich jeder Vorschlag an
    den zuletzt gespeicherten Monat — auch dann, wenn der selbst nur eine
    unverändert übernommene Kopie war. Die Kette liefe von Kopie zu Kopie,
    „übernommen aus …" nennte einen Monat, in dem nie jemand etwas abgelesen
    hat, und eine Korrektur am von Hand erfassten Monat erreichte die späteren
    Vorschläge nie. Genau darauf beruht der Weg, den dieses Modul anbietet:
    einmal von Hand erfassen, danach speist sich alles daraus.

    Gesucht wird in BEIDEN Stufen nur unter den Buchungen, die nicht nach dieser
    liegen — ein späterer Monat wüsste ja noch nichts von diesem. Die Schranke
    galt zeitweise nur in der ersten Stufe, mit zwei schrankenlosen Stufen
    dahinter: eine nachgetragene Buchung aus einem lange vergangenen Jahr bekam
    dann einen Vorschlag „übernommen aus" einem Monat, den es zu ihrer Zeit noch
    gar nicht gab. Fällt die Suche leer aus, ist das kein Mangel — dann greift
    der Jahreslohn, und sonst bleiben die Zeilen leer.

    Die eigene Buchung ist ausgeschlossen: sonst schlüge ein Vorschlag der
    Buchung ihre eigenen, schon gespeicherten Posten als fremde Herkunft vor und
    schriebe sie mit „übernommen aus <ihrem eigenen Monat>" auf GERECHNET um.
    """
    basis = (
        select(Lohnabrechnung.id, Transaction.date)
        .join(Transaction, Transaction.id == Lohnabrechnung.transaction_id)
        .where(Lohnabrechnung.transaction_id != tx.id, Transaction.date <= tx.date)
        .order_by(Transaction.date.desc(), Lohnabrechnung.id.desc())
    )
    selbst_erfasst = basis.where(
        Lohnabrechnung.id.in_(
            select(Lohnposten.abrechnung_id).where(Lohnposten.herkunft == LohnHerkunft.ERFASST)
        )
    )
    treffer = None
    for stmt in (selbst_erfasst, basis):
        treffer = db.execute(stmt).first()
        if treffer is not None:
            break
    if treffer is None:
        return None
    alt = db.scalar(
        select(Lohnabrechnung)
        .where(Lohnabrechnung.id == treffer[0])
        .options(selectinload(Lohnabrechnung.posten))
    )
    return (alt, treffer[1]) if alt is not None else None


def _jahreslohn(db: Session, jahr: int) -> tuple[Decimal, int] | None:
    """Bruttolohn eines Jahres aus der Verlaufsreihe — samt dem Jahr, das gilt.

    Gesucht wird zuerst das Jahr der Buchung. Fehlt es, gilt der jüngste Wert
    davor: ein Vorjahreslohn ist eine schlechtere Schätzung, aber eine
    nachvollziehbare — und sie ist als gerechnet gekennzeichnet.
    """
    reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == REIHE_JAHRESLOHN))
    if reihe is None:
        return None
    punkt = db.scalar(
        select(MetricPoint)
        .where(MetricPoint.series_id == reihe.id, MetricPoint.period_start <= date(jahr, 12, 31))
        .order_by(MetricPoint.period_start.desc())
        .limit(1)
    )
    if punkt is None or punkt.value <= 0:
        return None
    return punkt.value, punkt.period_start.year


def _pk_monatsbeitrag(db: Session) -> Decimal | None:
    """Monatsbeitrag an die Pensionskasse aus dem Vorsorgeausweis, falls erfasst."""
    reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == REIHE_VORSORGE))
    if reihe is None:
        return None
    punkt = db.scalar(
        select(MetricPoint)
        .where(MetricPoint.series_id == reihe.id)
        .order_by(MetricPoint.period_start.desc())
        .limit(1)
    )
    roh = (punkt.extras or {}).get(NEBENWERT_PK_MONAT) if punkt is not None else None
    if not roh:
        return None
    try:
        wert = Decimal(str(roh))
    except (ArithmeticError, ValueError):
        return None
    return _runden(wert) if wert > 0 else None


def vorschlag(db: Session, tx: Transaction) -> tuple[list[dict], str | None]:
    """Vorbelegte Editor-Zeilen und die Grundlage, aus der sie stammen.

    Drei Stufen, von der besten Quelle zur schlechtesten:

    1. **Ein früherer Monat**, den der Nutzer selbst erfasst hat. Seine
       abgelesenen Posten werden ``FORTGESCHRIEBEN`` übernommen, seine
       gerechneten bleiben ``GERECHNET`` (:func:`_fortgeschrieben`). Vorher
       wurde alles als gerechnet übernommen — das untertrieb den Normalfall:
       ein Lohnblatt gibt es nur bei einer Lohnänderung, und ein Monat
       dazwischen ist abgelesen, nur eben in einem früheren Monat.
    2. **Der Jahreslohn** aus der Verlaufsreihe, geteilt durch zwölf, plus die
       gesetzlichen Beitragssätze darauf. Das ist eine Schätzung und bleibt
       ``GERECHNET``.
    3. **Nichts.** Dann stehen die üblichen Zeilen leer da. Eine leere Zeile ist
       eine sichtbare Lücke; eine erfundene Zahl wäre eine unsichtbare.

    Die zurückgegebene Grundlage benennt die Stufe samt Herkunftsmonat
    („fortgeschrieben aus Juni 2026"). Sie ist der einzige Weg, an einer Zahl
    abzulesen, AUS WELCHEM Monat sie stammt — das Zeichen „=" sagt nur, DASS
    sie von anderswo kommt. Der Editor stellt sie darum als Text mit ins
    Formular, und die Anzeige an der Buchung wiederholt sie.

    NBUV, KTG, Pensionskasse und der Anteil am 13. Monatslohn stehen in den
    Stufen 2 und 3 als LEERE Zeile da: diese Sätze, der Kassenplan und der
    Vertrag hängen am Arbeitgeber, ein „üblicher" Wert wäre geraten. Die Zeile
    fehlt trotzdem nicht — sonst trüge der Nutzer sie jeden Monat neu und
    jedesmal anders benannt nach.
    """
    letzte = _letzte_abrechnung(db, tx)
    if letzte is not None:
        alt, alt_datum = letzte
        zeilen = [
            _zeile(p.label, p.art, p.betrag, _fortgeschrieben(p.herkunft))
            for p in alt.posten
        ]
        if zeilen:
            return zeilen, f"fortgeschrieben aus {MONATE[alt_datum.month - 1]} {alt_datum.year}"

    gerechnet = LohnHerkunft.GERECHNET
    jahr = _jahreslohn(db, tx.date.year)
    if jahr is not None:
        jahreswert, jahreszahl = jahr
        brutto = _runden(jahreswert / MONATE_IM_JAHR)
        zeilen = [_zeile("Bruttolohn", LohnPostenArt.BRUTTO, brutto, gerechnet)]
        zeilen += [
            _zeile(label, LohnPostenArt.BRUTTO, None, gerechnet)
            for label in BRUTTO_OHNE_QUELLE
        ]
        zeilen += [
            _zeile(label, LohnPostenArt.ABZUG, _runden(brutto * satz / 100), gerechnet)
            for label, satz in SAETZE
        ]
        pk = _pk_monatsbeitrag(db)
        zeilen += [
            _zeile(
                label,
                LohnPostenArt.ABZUG,
                pk if (label == "Pensionskasse" and pk is not None) else None,
                gerechnet,
            )
            for label in OHNE_QUELLE
        ]
        return zeilen, f"Jahreslohn {jahreszahl} ÷ 12"

    erfasst = LohnHerkunft.ERFASST
    leer = [_zeile("Bruttolohn", LohnPostenArt.BRUTTO, None, erfasst)]
    leer += [_zeile(label, LohnPostenArt.BRUTTO, None, erfasst) for label in BRUTTO_OHNE_QUELLE]
    leer += [_zeile(label, LohnPostenArt.ABZUG, None, erfasst) for label, _ in SAETZE]
    leer += [_zeile(label, LohnPostenArt.ABZUG, None, erfasst) for label in OHNE_QUELLE]
    return leer, None


# ---------------------------------------------------------------------------
# Speichern
# ---------------------------------------------------------------------------

# Rohwert aus dem Formular → Stufe. Ein unbekannter Wert fällt auf ERFASST
# zurück: was die App nicht als Ableitung wiedererkennt, gilt als vom Nutzer
# verantwortet. Andersherum bekäme ein Tippfehler im versteckten Feld ein „≈"
# geschenkt, und die Marke behauptete eine Herkunft, die niemand kennt.
_ROHE_STUFE: dict[str, LohnHerkunft] = {stufe.value: stufe for stufe in _STUFEN}


def herkunft_nach_aenderung(
    betrag: Decimal, alt_betrag: Decimal | None, alt_herkunft: str
) -> LohnHerkunft:
    """Bestimmt, ob ein gespeicherter Posten als erfasst oder gerechnet gilt.

    Die Regel ist bewusst mechanisch und nicht abfragbar: **wer eine Zahl
    ändert, verantwortet sie** — sie gilt danach als erfasst. Ein Betrag, der
    unverändert aus dem Vorschlag stammt, bleibt gerechnet.

    Tippt der Nutzer aus Versehen exakt den vorgeschlagenen Wert nach, behält
    der Posten seine bisherige Stufe. Das ist die harmlose Richtung: die Anzeige
    behauptet dann weniger Genauigkeit, als vorhanden wäre — nie mehr.

    Ein unveränderter Posten behält seine Stufe GENAU, statt auf „gerechnet"
    zusammenzufallen: sonst verlöre ein fortgeschriebener Monat seine Stufe beim
    ersten Speichern wieder, und die Unterscheidung hielte keinen einzigen
    Durchlauf durch das Formular aus.
    """
    if alt_betrag is None or betrag != alt_betrag:
        return LohnHerkunft.ERFASST
    return _ROHE_STUFE.get(alt_herkunft, LohnHerkunft.ERFASST)


def speichern(
    db: Session,
    tx: Transaction,
    zeilen: list[tuple[LohnPostenArt, str, Decimal, LohnHerkunft]],
    *,
    grundlage: str | None,
) -> Lohnabrechnung:
    """Ersetzt die Aufstellung einer Buchung durch die übergebenen Posten.

    Ersetzen und nicht abgleichen: die Posten sind eine Momentaufnahme, keine
    Stammdaten. Ein Abgleich müsste raten, welche Zeile welcher entspricht,
    sobald der Nutzer eine umbenennt.

    Ausdrücklich KEINE Prüfung, ob der Nettolohn dem gebuchten Betrag
    entspricht. Bei geschätzten Werten tut er das fast nie — eine solche Prüfung
    würde entweder das Speichern verhindern oder zum Zurechtbiegen der Zahlen
    einladen. Die Differenz wird stattdessen angezeigt.
    """
    abrechnung = abrechnung_zu(db, tx.id)
    if abrechnung is None:
        abrechnung = Lohnabrechnung(transaction_id=tx.id)
        db.add(abrechnung)
        db.flush()
    abrechnung.grundlage = grundlage
    abrechnung.posten.clear()
    db.flush()
    for i, (art, label, betrag, herkunft) in enumerate(zeilen):
        abrechnung.posten.append(
            Lohnposten(art=art, label=label, betrag=betrag, herkunft=herkunft, sort_order=i * 10)
        )
    return abrechnung


def entfernen(db: Session, tx_id: int) -> bool:
    """Löscht die Aufstellung einer Buchung. ``False``, wenn es keine gab."""
    abrechnung = abrechnung_zu(db, tx_id)
    if abrechnung is None:
        return False
    db.delete(abrechnung)
    return True


# ---------------------------------------------------------------------------
# Die Jahresprobe: der einzige Ort, an dem sich eine Fortschreibung widerlegen lässt
# ---------------------------------------------------------------------------
#
# Eine fortgeschriebene Zahl ist abgelesen — nur eben in einem früheren Monat.
# Ob sie HEUTE noch stimmt, sagt ihr niemand an: dazwischen liegt genau der Fall,
# für den es kein Blatt gibt. Der Lohnausweis schliesst diese Lücke, weil er ein
# ganzes Jahr in EINER Zahl nennt.
#
# **Verglichen wird BRUTTO gegen BRUTTO, und das ist keine Bequemlichkeit.**
# Die naheliegende Probe wäre die Netto-Probe (Summe der Monatsnetti gegen den
# Nettolohn des Ausweises). Sie ist mit diesen Daten nicht ehrlich zu bauen:
#
# * Die Reihe ``lohn`` führt den Jahres-BRUTTOLOHN. Einen Netto-Jahreswert gibt
#   es nirgends — die Probe hätte keine rechte Seite.
# * Auch nachgetragen hülfe er nicht. Der Nettolohn des Ausweises zieht die
#   Sozialversicherungs- und Vorsorgebeiträge ab, aber nicht jeden Abzug, der
#   auf einem Lohnblatt steht (Krankentaggeld, Quellensteuer). Die App rechnet
#   ihren Nettolohn dagegen aus ALLEN erfassten Abzügen. Die Probe müsste die
#   frei benannten Abzugszeilen danach sortieren, welche im Ausweis stecken —
#   eine Zuordnung auf Verdacht, an der jede Umbenennung scheitert.
#
# Die Brutto-Seite braucht nichts davon: der Jahreswert und die Brutto-Posten
# der Monate stehen auf derselben Seite derselben Abrechnung.
#
# **Die unregelmässigen Leistungen** (Bonus, Weihnachtszulage) sind der eine
# Fall, an dem eine naive Probe scheitern MUSS: sie stecken im Jahreswert, aber
# auf keinem Monatsblatt — die Probe meldete jedes Jahr eine Abweichung, die
# keine ist, und wäre nach dem zweiten Mal Tapete. Gelöst wird das nicht durch
# einen Ausnahme-Nebenwert, sondern dadurch, dass die Probe ZAHLUNGEN zählt und
# nicht Monate: eine Sonderzahlung ist eine eigene Gutschrift und bekommt wie
# jede andere ihre Aufstellung. Wer sie erfasst, bringt das Jahr zum Aufgehen —
# einmal, statt jedes Jahr eine Meldung wegzuklicken. Solange sie fehlt, sagt
# der Befund genau das als ersten möglichen Grund.

# Ab welcher Abweichung ein Jahr als „geht nicht auf" gilt. Zwölf auf Rappen
# gerundete Monate können sich um ein paar Rappen verfehlen; ein Franken lässt
# dafür Luft und liegt weit unter jedem echten Fehlbetrag. Derselbe Wert wie im
# Soll/Ist-Abgleich der Verlaufsseite — zwei verschiedene Schwellen für dieselbe
# Frage wären zwei verschiedene Antworten.
JAHRES_TOLERANZ = Decimal("1.00")


@dataclass(frozen=True)
class Jahresprobe:
    """Summe der Monats-Brutti eines Jahres gegen den Bruttolohn des Lohnausweises."""

    jahr: int
    #: Jahreswert der Reihe ``lohn`` — der Bruttolohn laut Ausweis.
    ausweis: Decimal
    #: Summe der Brutto-Posten aller Aufstellungen dieses Jahres.
    summe: Decimal
    #: Wie viele Gutschriften des Jahres eine Aufstellung tragen.
    gutschriften: int
    #: Trägt mindestens eine davon einen fortgeschriebenen Posten?
    fortgeschrieben: bool

    @property
    def differenz(self) -> Decimal:
        """Summe minus Ausweis — negativ heisst: es fehlt Brutto."""
        return _runden(self.summe - self.ausweis)

    @property
    def geht_auf(self) -> bool:
        return abs(self.differenz) <= JAHRES_TOLERANZ

    @property
    def befund(self) -> str:
        """Was die Zahlen bedeuten — knapp, weil die Zeile im Fuss eines Blocks steht.

        Der Text steht hier und nicht in der Vorlage: er verzweigt nach Richtung
        und danach, ob überhaupt etwas fortgeschrieben ist, und diese Verzweigung
        gehört nicht in eine Schleife über Buchungszeilen.

        Die Zahl der Gutschriften steht IMMER dabei — sie ist das Mass dafür, ob
        die Summe überhaupt etwas heisst. Der mögliche Grund steht nur bei einer
        Abweichung, und nur der, der zur Richtung passt: eine fehlende
        Sonderzahlung kann die Summe nicht zu hoch machen. Auf 375px sind das
        rund zweieinhalb Zeilen; jeder weitere Halbsatz wäre eine Zeile mehr im
        längsten Element der Buchungsliste.
        """
        kopf = f"{self.jahr}: {self.gutschriften} Gutschriften"
        if self.geht_auf:
            return f"{kopf} ergeben den Lohnausweis."
        abstand = chf_wert(abs(self.differenz))
        if self.differenz < 0:
            frage = "Sonderzahlung ohne Aufstellung"
            if self.fortgeschrieben:
                frage += " oder fortgeschriebener Monat zu tief"
            return f"{kopf}, {abstand} unter dem Lohnausweis. {frage}?"
        if self.fortgeschrieben:
            return (f"{kopf}, {abstand} über dem Lohnausweis. "
                    "Fortgeschriebener Monat zu hoch?")
        return f"{kopf}, {abstand} über dem Lohnausweis."


def _jahreswert_genau(db: Session, jahre: set[int]) -> dict[int, Decimal]:
    """Bruttolohn laut Ausweis, je Jahr — nur Punkte, die IM Jahr beginnen.

    Anders als :func:`_jahreslohn` gibt es hier keinen Rückfall auf den letzten
    Wert davor. Ein Vorjahreswert taugt als Schätzgrundlage für einen Monat, aber
    nicht als Gegenprobe: das Jahr, gegen das gerechnet wird, muss dasselbe sein,
    sonst behauptet eine Abweichung etwas über ein Jahr, dessen Ausweis niemand
    erfasst hat.
    """
    if not jahre:
        return {}
    reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == REIHE_JAHRESLOHN))
    if reihe is None:
        return {}
    punkte = db.scalars(
        select(MetricPoint).where(
            MetricPoint.series_id == reihe.id,
            MetricPoint.period_start >= date(min(jahre), 1, 1),
            MetricPoint.period_start <= date(max(jahre), 12, 31),
        )
    )
    return {
        p.period_start.year: p.value
        for p in punkte
        if p.period_start.year in jahre and p.value > 0
    }


def jahresproben(
    db: Session, jahre: set[int], *, heute: date | None = None
) -> dict[int, Jahresprobe]:
    """Die Probe je Jahr — nur für ABGESCHLOSSENE Jahre mit Ausweiswert und Aufstellungen.

    Ein Jahr ohne Ausweiswert bekommt keinen Eintrag und die Anzeige damit keine
    Zeile: eine Probe ohne rechte Seite hätte nichts zu sagen, und ein Satz, der
    nur mitteilt, dass er nichts mitteilt, ist genau der Fülltext, den diese
    Oberfläche nicht führt.

    **Das laufende Jahr ebenso wenig.** Der Ausweiswert ist ein JAHRESwert; ihm
    stünden im August sieben Gutschriften gegenüber. Die Probe ginge damit jeden
    Monat nicht auf und meldete einen Fehler, der keiner ist — bis Dezember, wo
    sie plötzlich stimmte. Eine Warnung, die elf von zwölf Monaten falsch ist,
    bringt niemandem bei, im zwölften hinzusehen.
    """
    grenze = (heute or heute_lokal()).year
    jahre = {j for j in jahre if j < grenze}
    if not jahre:
        return {}
    ausweise = _jahreswert_genau(db, jahre)
    if not ausweise:
        return {}
    # Das Buchungsdatum reist als eigene Spalte mit: ``Lohnabrechnung`` hat keine
    # Beziehung zur Buchung, und eine dafür anzulegen hiesse, das Modell für eine
    # Auswertung zu ändern.
    zeilen = db.execute(
        select(Lohnabrechnung, Transaction.date)
        .join(Transaction, Transaction.id == Lohnabrechnung.transaction_id)
        .where(
            Transaction.date >= date(min(ausweise), 1, 1),
            Transaction.date <= date(max(ausweise), 12, 31),
        )
        .options(selectinload(Lohnabrechnung.posten))
    )
    summen: dict[int, Decimal] = {}
    anzahl: dict[int, int] = {}
    fortgeschrieben: set[int] = set()
    for a, tag in zeilen:
        jahr = tag.year
        if jahr not in ausweise:
            continue
        brutto = sum(
            (p.betrag for p in a.posten if p.art == LohnPostenArt.BRUTTO), Decimal("0")
        )
        summen[jahr] = summen.get(jahr, Decimal("0")) + brutto
        anzahl[jahr] = anzahl.get(jahr, 0) + 1
        if any(p.herkunft == LohnHerkunft.FORTGESCHRIEBEN for p in a.posten):
            fortgeschrieben.add(jahr)
    return {
        jahr: Jahresprobe(
            jahr=jahr,
            ausweis=wert,
            summe=_runden(summen[jahr]),
            gutschriften=anzahl[jahr],
            fortgeschrieben=jahr in fortgeschrieben,
        )
        for jahr, wert in ausweise.items()
        if jahr in anzahl
    }
