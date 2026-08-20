"""Verlaufsreihen lesen, schreiben und für die Darstellung aufbereiten.

Die Reihen liegen bewusst neben den Buchungen (siehe ``models.MetricSeries``).
Dieser Dienst ist die einzige Stelle, die auf ``metric_points`` schreibt —
Import und Handerfassung gehen beide durch :func:`setze_punkt`, damit es nur
eine Regel gibt, was beim Überschreiben passiert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from moneten.db.models import MetricCadence, MetricPoint, MetricSeries, MetricUnit


@dataclass(frozen=True)
class Punkt:
    """Ein Messwert, aufbereitet für die Anzeige."""

    id: int
    start: date
    ende: date
    wert: Decimal
    neben: Decimal | None
    extras: dict[str, str]
    quelle: str | None
    notiz: str | None
    # Veränderung zum vorherigen Punkt in Prozent; None beim ersten.
    diff_pct: Decimal | None


@dataclass(frozen=True)
class Verlauf:
    """Eine Reihe samt ihrer Punkte und den daraus abgeleiteten Kennzahlen."""

    reihe: MetricSeries
    punkte: list[Punkt]

    @property
    def leer(self) -> bool:
        return not self.punkte

    @property
    def aktuell(self) -> Punkt | None:
        return self.punkte[-1] if self.punkte else None

    @property
    def erster(self) -> Punkt | None:
        return self.punkte[0] if self.punkte else None

    @property
    def gesamt_pct(self) -> Decimal | None:
        """Veränderung vom ersten zum letzten Wert in Prozent."""
        if len(self.punkte) < 2:
            return None
        a, b = self.punkte[0].wert, self.punkte[-1].wert
        if a == 0:
            return None
        return ((b - a) / a * 100).quantize(Decimal("0.1"))


def reihen(db: Session, *, mit_archivierten: bool = False) -> list[MetricSeries]:
    """Alle Reihen in Anzeigereihenfolge."""
    stmt = select(MetricSeries).order_by(MetricSeries.sort_order, MetricSeries.name)
    if not mit_archivierten:
        stmt = stmt.where(MetricSeries.archived.is_(False))
    return list(db.scalars(stmt))


def archiviere_reihe(db: Session, slug: str, *, archiviert: bool) -> MetricSeries | None:
    """Blendet eine Reihe aus der Verlaufsseite aus — oder holt sie zurück.

    **Archivieren und nicht löschen.** An der Reihe hängen ihre Messwerte mit
    ``cascade="all, delete-orphan"``; ein echtes Löschen nähme Jahre von Hand
    eingetragener Werte lautlos mit. Ausserdem legt der Seed jede Reihe, deren
    Slug fehlt, beim nächsten Start neu an — gelöscht wäre sie also nur bis zum
    Neustart weg, die Werte dagegen für immer.

    Das ``archived``-Feld gab es längst, gelesen wurde es auch (``reihen``,
    ``alle_verlaeufe``) — geschrieben hat es nur nie jemand. Ein deutscher
    Nutzer behielt damit „Direkte Bundessteuer" dauerhaft auf der Seite, und
    zwar auffällig: eine leere Reihe klappt ihr Nachtrag-Formular von selbst auf
    und fordert zur Benutzung auf.

    Der Import füllt weiterhin auch archivierte Reihen (siehe
    ``routers.metrics``) — sonst klaffte beim Zurückholen eine Lücke.
    """
    reihe = db.scalar(select(MetricSeries).where(MetricSeries.slug == slug))
    if reihe is None:
        return None
    reihe.archived = archiviert
    db.add(reihe)
    return reihe


def reihe_nach_slug(db: Session, slug: str) -> MetricSeries | None:
    """Eine Reihe über ihren stabilen Schlüssel."""
    return db.scalar(select(MetricSeries).where(MetricSeries.slug == slug))


def verlauf(db: Session, reihe: MetricSeries) -> Verlauf:
    """Punkte einer Reihe, chronologisch, mit Veränderung zum Vorwert."""
    roh = list(db.scalars(
        select(MetricPoint)
        .where(MetricPoint.series_id == reihe.id)
        .order_by(MetricPoint.period_start)
    ))
    punkte: list[Punkt] = []
    vorher: Decimal | None = None
    for p in roh:
        extras = dict(p.extras or {})
        neben = None
        if reihe.secondary_key and (roh_neben := extras.get(reihe.secondary_key)):
            try:
                neben = Decimal(roh_neben)
            except (ArithmeticError, ValueError):
                neben = None
        diff = None
        if vorher is not None and vorher != 0:
            diff = ((p.value - vorher) / vorher * 100).quantize(Decimal("0.1"))
        punkte.append(Punkt(
            id=p.id, start=p.period_start, ende=p.period_end, wert=p.value,
            neben=neben, extras=extras, quelle=p.source, notiz=p.note, diff_pct=diff,
        ))
        vorher = p.value
    return Verlauf(reihe=reihe, punkte=punkte)


def alle_verlaeufe(db: Session) -> list[Verlauf]:
    """Alle nicht archivierten Reihen samt Punkten — ein Rundgang, keine N+1."""
    stmt = (
        select(MetricSeries)
        .where(MetricSeries.archived.is_(False))
        .options(selectinload(MetricSeries.points))
        .order_by(MetricSeries.sort_order, MetricSeries.name)
    )
    return [verlauf(db, r) for r in db.scalars(stmt)]


def setze_punkt(
    db: Session,
    reihe: MetricSeries,
    *,
    start: date,
    ende: date,
    wert: Decimal,
    extras: dict[str, str] | None = None,
    quelle: str | None = None,
    notiz: str | None = None,
    ueberschreiben: bool = True,
) -> tuple[MetricPoint, bool]:
    """Legt einen Punkt an oder aktualisiert den bestehenden derselben Periode.

    Gibt ``(Punkt, war_neu)`` zurück. Mit ``ueberschreiben=False`` bleibt ein
    vorhandener Wert unangetastet — so kann der Einmal-Import beliebig oft
    laufen, ohne von Hand nachgetragene Korrekturen wieder plattzumachen.

    Die Periode ist der Schlüssel, nicht die Quelle: trägt man einen Monat
    von Hand nach, den später doch noch ein Beleg liefert, soll daraus ein
    Wert werden und nicht zwei.
    """
    vorhanden = db.scalar(
        select(MetricPoint).where(
            MetricPoint.series_id == reihe.id,
            MetricPoint.period_start == start,
        )
    )
    if vorhanden is not None:
        if ueberschreiben:
            vorhanden.period_end = ende
            vorhanden.value = wert
            vorhanden.extras = extras or None
            vorhanden.source = quelle
            vorhanden.note = notiz
        return vorhanden, False

    punkt = MetricPoint(
        series_id=reihe.id,
        period_start=start,
        period_end=ende,
        value=wert,
        extras=extras or None,
        source=quelle,
        note=notiz,
    )
    db.add(punkt)
    return punkt, True


def loesche_punkt(db: Session, punkt_id: int) -> bool:
    """Entfernt einen Messwert. ``False``, wenn es ihn nicht (mehr) gibt."""
    punkt = db.get(MetricPoint, punkt_id)
    if punkt is None:
        return False
    db.delete(punkt)
    return True


def periode_aus_takt(takt: MetricCadence, start: date) -> date:
    """Passendes Perioden-Ende zum Takt einer Reihe.

    Die Handerfassung fragt nur nach dem Beginn — alles andere wäre Tipparbeit
    für etwas, das sich aus dem Takt ergibt.
    """
    if takt == MetricCadence.MONATLICH:
        return _monatsende(start)
    if takt == MetricCadence.QUARTALSWEISE:
        letzter_monat = start.month + 2
        jahr = start.year + (letzter_monat - 1) // 12
        return _monatsende(date(jahr, (letzter_monat - 1) % 12 + 1, 1))
    if takt == MetricCadence.JAEHRLICH:
        return date(start.year, 12, 31)
    return start  # UNREGELMAESSIG: Stichtag, keine Spanne


def periode_text(takt: MetricCadence, start: date, ende: date) -> str:
    """Periode als Text, so kurz wie der Takt es zulässt.

    Dieselben Formen wie das Makro ``periode`` im Template. Beide stehen auf
    derselben Seite nebeneinander — Werteliste, Bestätigungsliste nach dem
    Import und die Beschriftung der Balken —, und zwei Schreibweisen für
    dieselbe Periode wären dort ein Widerspruch.
    """
    if takt == MetricCadence.MONATLICH:
        return start.strftime("%m.%Y")
    if takt == MetricCadence.JAEHRLICH:
        return start.strftime("%Y")
    if takt == MetricCadence.QUARTALSWEISE:
        return f"Q{(start.month + 2) // 3} {start.strftime('%Y')}"
    if ende == start:
        return start.strftime("%d.%m.%Y")
    return f"{start.strftime('%d.%m.%y')}–{ende.strftime('%d.%m.%y')}"


def _monatsende(tag: date) -> date:
    """Letzter Tag des Monats, in dem ``tag`` liegt."""
    if tag.month == 12:
        return date(tag.year, 12, 31)
    return date.fromordinal(date(tag.year, tag.month + 1, 1).toordinal() - 1)


def formatiere(wert: Decimal, einheit: MetricUnit) -> str:
    """Zahl mit Einheit, wie sie in der Oberfläche stehen soll."""
    if einheit == MetricUnit.CHF:
        return f"{wert:,.2f}".replace(",", "'")
    if einheit == MetricUnit.KWH:
        return f"{wert:,.0f} kWh".replace(",", "'")
    return f"{wert:.1f} %"
