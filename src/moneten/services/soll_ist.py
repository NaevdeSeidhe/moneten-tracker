"""Stimmt, was der Beleg verlangt, mit dem überein, was gebucht wurde?

Die Verlaufsreihen sagen, was eine Leistung **kosten sollte** (Prämienabrechnung,
Mietvertrag, Stromrechnung). Die Buchungen sagen, was **tatsächlich abging**.
Dieser Dienst stellt beides nebeneinander und beantwortet drei Fragen:

1. Ist die Zahlung überhaupt gebucht?
2. Ist sie in der richtigen Kategorie gebucht?
3. Bei laufenden Kosten: ist sie als Abo/Fixposten erfasst — und steht dort noch
   der alte Betrag, obwohl der Beleg längst einen neuen nennt?

Frage 3 ist die, die im Alltag Geld kostet. Eine Prämie steigt zum Jahreswechsel,
der Fixposten in der App bleibt auf dem Vorjahreswert stehen, und das Budget
rechnet ab dann dauerhaft zu tief — ohne dass irgendetwas Alarm schlägt.

**Was dieser Dienst NICHT tut:** er ändert nichts. Er stellt fest und meldet.
Ob eine Abweichung ein Fehler ist, weiss nur der Mensch davor — eine Prämie kann im Januar
zweimal abgehen, weil der Dezember spät belastet wurde, und das ist dann richtig.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import (
    BudgetInterval,
    Category,
    ManualSubscription,
    MetricCadence,
    MetricKind,
    MetricSeries,
)
from moneten.services.metrics import Verlauf
from moneten.services.splits import effective_category_amounts

# Ab welcher Abweichung eine Periode als „passt nicht" gilt. Rappenbeträge
# entstehen durch Rundung und Teilmonate; darunter wäre jede Meldung Lärm.
TOLERANZ = Decimal("1.00")

# Wie stark der hinterlegte Abo-Betrag vom aktuellen Belegwert abweichen darf,
# bevor er als veraltet gilt. Bewusst enger als TOLERANZ: hier vergleichen wir
# zwei Sollwerte miteinander, da gibt es keinen Grund für Abweichungen.
ABO_TOLERANZ = Decimal("0.50")


@dataclass(frozen=True)
class PeriodenBefund:
    """Soll und Ist einer einzelnen Periode."""

    start: date
    ende: date
    soll: Decimal
    ist: Decimal
    # Anzahl Buchungen, die zum Ist beigetragen haben. Null heisst: nichts gebucht.
    buchungen: int

    @property
    def differenz(self) -> Decimal:
        return self.ist - self.soll

    @property
    def fehlt(self) -> bool:
        return self.buchungen == 0

    @property
    def passt(self) -> bool:
        return not self.fehlt and abs(self.differenz) <= TOLERANZ


@dataclass(frozen=True)
class AboBefund:
    """Zustand des laufenden Postens zu einer Reihe."""

    erfasst: bool
    name: str | None
    hinterlegt: Decimal | None
    erwartet: Decimal | None
    art: str | None  # "abo" | "fix"

    @property
    def veraltet(self) -> bool:
        """Steht im Fixposten ein anderer Betrag, als der Beleg heute nennt?"""
        if self.hinterlegt is None or self.erwartet is None:
            return False
        return abs(self.hinterlegt - self.erwartet) > ABO_TOLERANZ


@dataclass(frozen=True)
class Abgleich:
    """Ergebnis für eine Reihe."""

    reihe: MetricSeries
    kategorie: Category | None
    perioden: list[PeriodenBefund]
    abo: AboBefund | None
    # Warum kein Abgleich möglich war — leer, wenn er stattgefunden hat.
    grund: str = ""

    @property
    def geprueft(self) -> bool:
        return not self.grund

    @property
    def anzahl_fehlend(self) -> int:
        return sum(1 for p in self.perioden if p.fehlt)

    @property
    def anzahl_abweichend(self) -> int:
        return sum(1 for p in self.perioden if not p.fehlt and not p.passt)

    @property
    def sauber(self) -> bool:
        """Alles gebucht, alles im Rahmen, Abo aktuell."""
        if not self.geprueft or not self.perioden:
            return False
        if self.anzahl_fehlend or self.anzahl_abweichend:
            return False
        return not (self.abo and (not self.abo.erfasst or self.abo.veraltet))


def _monatsanfang(tag: date) -> date:
    return tag.replace(day=1)


def _monat_danach(tag: date) -> date:
    """Erster Tag des Folgemonats — als offene obere Grenze."""
    return date(tag.year + 1, 1, 1) if tag.month == 12 else date(tag.year, tag.month + 1, 1)


def abgleich(db: Session, verlauf: Verlauf) -> Abgleich:
    """Vergleicht die Punkte einer Reihe mit den Buchungen ihrer Kategorie."""
    reihe = verlauf.reihe

    if reihe.kind == MetricKind.VERMOEGEN:
        return Abgleich(reihe, None, [], None,
                        grund="Bestandsgrösse — dazu gibt es keine Buchung.")
    if reihe.category_id is None:
        return Abgleich(reihe, None, [], None,
                        grund="Keiner Kategorie zugeordnet.")
    kategorie = db.get(Category, reihe.category_id)
    if kategorie is None:
        return Abgleich(reihe, None, [], None,
                        grund="Die verknüpfte Kategorie gibt es nicht mehr.")
    if not verlauf.punkte:
        return Abgleich(reihe, kategorie, [], _abo_befund(db, reihe, None),
                        grund="Noch keine Werte erfasst.")

    # Alle Buchungen des gesamten abgedeckten Zeitraums in EINEM Zug holen.
    # Je Periode einzeln zu fragen wäre bei 70 Prämienmonaten 70 Abfragen.
    von = _monatsanfang(min(p.start for p in verlauf.punkte))
    bis = _monat_danach(max(p.ende for p in verlauf.punkte))
    zeilen = effective_category_amounts(db, date_from=von, date_to=bis)
    eigene = [(betrag, tag) for kat, betrag, tag in zeilen if kat == reihe.category_id]

    perioden: list[PeriodenBefund] = []
    for punkt in verlauf.punkte:
        # Die obere Grenze ist offen und liegt am Monatsanfang nach dem
        # Perioden-Ende: eine Prämie für Februar wird oft erst Ende Januar oder
        # Anfang Februar belastet. Auf den Tag genau zu schneiden hiesse, einen
        # Grossteil der real vorhandenen Buchungen als „fehlt" zu melden.
        p_von = _monatsanfang(punkt.start)
        p_bis = _monat_danach(punkt.ende)
        treffer = [b for b, tag in eigene if p_von <= tag < p_bis]
        # Ausgaben stehen negativ in den Buchungen, der Belegwert positiv.
        ist = -sum(treffer, Decimal("0")) if reihe.kind == MetricKind.AUSGABE \
            else sum(treffer, Decimal("0"))
        perioden.append(PeriodenBefund(
            start=punkt.start, ende=punkt.ende, soll=punkt.wert,
            ist=ist, buchungen=len(treffer),
        ))

    erwartet = verlauf.aktuell.wert if verlauf.aktuell else None
    return Abgleich(reihe, kategorie, perioden, _abo_befund(db, reihe, erwartet))


def _abo_befund(db: Session, reihe: MetricSeries, erwartet: Decimal | None) -> AboBefund | None:
    """Gibt es zu dieser Reihe einen laufenden Posten, und ist sein Betrag aktuell?

    Nur für Reihen mit regelmässigem Takt: eine Steuerveranlagung oder ein
    Vorsorgeausweis ist kein Abo, dort wäre die Frage sinnlos.
    """
    if reihe.cadence == MetricCadence.UNREGELMAESSIG and reihe.slug != "miete":
        return None
    if reihe.kind != MetricKind.AUSGABE:
        return None

    abo = db.scalar(
        select(ManualSubscription).where(ManualSubscription.category_id == reihe.category_id)
    )
    if abo is None:
        return AboBefund(erfasst=False, name=None, hinterlegt=None,
                         erwartet=erwartet, art=None)

    # Auf denselben Takt bringen, sonst vergleicht man Monat gegen Jahr.
    hinterlegt = abo.amount
    if abo.interval == BudgetInterval.JAEHRLICH and reihe.cadence == MetricCadence.MONATLICH:
        hinterlegt = (hinterlegt / 12).quantize(Decimal("0.01"))
    elif abo.interval == BudgetInterval.MONATLICH and reihe.cadence == MetricCadence.JAEHRLICH:
        hinterlegt = hinterlegt * 12
    elif reihe.cadence not in (MetricCadence.MONATLICH, MetricCadence.JAEHRLICH):
        # Quartalsweise (Strom) und Stichtag (Miete) lassen sich auf KEIN
        # Budget-Intervall abbilden. Ohne diesen Zweig verglich der Abgleich
        # einen Monatsbetrag mit einem Quartalswert und meldete darum dauerhaft
        # „veraltet" — und der Knopf daneben hätte den Quartalsbetrag in einen
        # monatlichen Posten geschrieben.
        #
        # ``erwartet=None`` statt eines geratenen Umrechnungsfaktors: eine
        # Stromrechnung deckt drei Monate ab, aber nicht gleichmässig, und
        # welchen Betrag ein monatlicher Posten tragen soll, entscheidet nicht
        # die App. Ob ein Posten EXISTIERT, bleibt die Meldung wert.
        return AboBefund(erfasst=True, name=abo.name, hinterlegt=abo.amount,
                         erwartet=None, art=abo.kind)

    return AboBefund(erfasst=True, name=abo.name, hinterlegt=hinterlegt,
                     erwartet=erwartet, art=abo.kind)


def alle_abgleiche(db: Session, verlaeufe: list[Verlauf]) -> list[Abgleich]:
    """Abgleich für alle Reihen, auffällige zuerst.

    Die Sortierung ist der halbe Nutzen: bei elf Reihen soll man nicht suchen
    müssen, wo etwas nicht stimmt.
    """
    ergebnisse = [abgleich(db, v) for v in verlaeufe]
    return sorted(
        ergebnisse,
        key=lambda a: (
            a.sauber,                       # Unsaubere zuerst
            not a.geprueft,                 # Ungeprüfte danach
            -(a.anzahl_fehlend + a.anzahl_abweichend),
            a.reihe.sort_order,
        ),
    )
