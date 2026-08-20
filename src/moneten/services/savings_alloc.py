"""Verteilung eines Kontosaldos auf mehrere Sparziele.

Liegt bewusst als eigener Service und nicht im Router: der Kündigungs-Rechner
braucht dieselbe Rechnung. Zwei Stellen, die „angespart" unterschiedlich
ermitteln, widersprechen sich früher oder später — genau dieser Fehler steckte
schon einmal in der App (jedes Ziel bekam den vollen Kontosaldo).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from moneten.db.models import GoalPriority, SavingsGoal

# Semantische Reihenfolge. `priority` liegt als String in der DB, ein
# ORDER BY sortiert deshalb alphabetisch — also „high, low, medium".
_PRIO_ORDER = {GoalPriority.HIGH: 0, GoalPriority.MEDIUM: 1, GoalPriority.LOW: 2}


def _sort_key(g: SavingsGoal) -> tuple:
    """ANZEIGE-Reihenfolge: Erledigte nach hinten, dann Priorität, dann Zieldatum."""
    return (
        g.is_achieved,
        _PRIO_ORDER.get(g.priority, 9),
        g.target_date or date.max,
        g.id or 0,
    )


def _alloc_key(g: SavingsGoal) -> tuple:
    """VERTEIL-Reihenfolge: erreichte Ziele zuerst, sonst wie die Anzeige.

    Bewusst andere Reihenfolge als :func:`_sort_key`. Für ein erreichtes Ziel
    gilt sein Geld als gebunden, es wird also zuerst vom Saldo abgezogen. Die
    Gegenannahme (erreichte Ziele verbrauchen nichts, weil das Geld schon
    ausgegeben wurde) wäre genauso denkbar, würde aber offene Ziele zu voll
    darstellen — und zu viel verfügbares Geld auszuweisen ist in einer
    Budget-App der teurere Irrtum.
    """
    return (not g.is_achieved, _PRIO_ORDER.get(g.priority, 9), g.target_date or date.max, g.id or 0)


def allocate_savings(goals: list[SavingsGoal], balances: dict[int, Decimal]) -> dict[int, Decimal]:
    """Verteilt jeden Kontosaldo auf die Ziele, die an diesem Konto hängen.

    Vorher bekam **jedes** Ziel den vollen Kontosaldo als „angespart" — hingen
    zwei Ziele am selben Sparkonto, zählte die Seitensumme dessen Guthaben
    doppelt. Bei den Testdaten ergab das „angespart 25'000" bei 14'000 Ziel.

    Verteilt wird als Wasserfall in der Reihenfolge :func:`_alloc_key`: das erste
    Ziel bekommt so viel, wie es braucht, der Rest fliesst ans nächste. Das
    entspricht der Denkweise „mein Notgroschen ist voll, was übrig ist, spare
    ich auf die Ferien". Nebeneffekt: kein Ziel kann mehr als seinen Zielbetrag
    ausweisen, und die Summe über alle Ziele eines Kontos bleibt ≤ Saldo.

    :param balances: Kontosaldo je ``account_id`` (negative gelten als 0).
    :return: zugeteilter Betrag je ``goal.id``; Ziele ohne Konto fehlen.
    """
    rest = {aid: max(bal, Decimal("0")) for aid, bal in balances.items()}
    zuteilung: dict[int, Decimal] = {}
    for g in sorted(goals, key=_alloc_key):
        if g.account_id is None or g.account_id not in rest:
            continue
        ziel = g.target_amount or Decimal("0")
        anteil = min(rest[g.account_id], ziel) if ziel > 0 else Decimal("0")
        zuteilung[g.id] = anteil
        rest[g.account_id] -= anteil
    return zuteilung


