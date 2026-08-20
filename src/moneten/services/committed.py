"""Was vom „Noch frei" schon vergeben ist.

Die Leitzahl auf der Budget-Seite (``soll - ist``) beantwortet „wie viel Luft
habe ich noch" — aber sie sagt nichts darüber, dass Miete, Handy und Netflix
diesen Monat noch abgehen. Mitte Monat sieht es dadurch entspannter aus, als es
ist, und am Zahltag kippt die Zahl ohne Vorwarnung.

Diese Rechnung stellt den bereits vergebenen Teil daneben. Sie fügt der Budget-
Seite **keine** neue Wahrheit hinzu, sondern zieht sie nur vor: was hier
abgezogen wird, hätte die Leitzahl ohnehin verloren, sobald die Buchung kommt.

Drei bewusste Festlegungen:

* **Monatsäquivalent, nicht Cash-Betrag.** Ein Jahresabo zählt mit 1/12, weil
  die Budget-Seite auf der Soll-Seite genauso rechnet („inkl. 1/12 der
  Jahreskosten"). Der volle Jahresbetrag hier würde zwei Rechenwelten mischen.
* **Kein Doppelzählen.** Ist die Zahlung diesen Monat schon gebucht, steckt sie
  bereits im Ist — dann wird nichts mehr abgezogen.
* **Nur der laufende und künftige Monat.** In einem abgelaufenen Monat steht
  nichts mehr aus; dort wäre jede Prognose nachträglich erfunden.
"""

from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months, heute_lokal
from moneten.db.models import BudgetInterval, ManualSubscription, Transaction
from moneten.services.median_budget import monthly_equivalent
from moneten.services.subscriptions import _merchant_key

# Ein Betrag gilt als „dieselbe Zahlung", wenn er höchstens so weit abweicht.
# 15 % fangen Kursschwankungen und Teuerungsanpassungen ab, ohne eine
# Wocheneinkaufs-Buchung fälschlich als Abo-Zahlung durchgehen zu lassen.
_BETRAG_TOLERANZ = Decimal("0.15")


def _schluessel(sub: ManualSubscription) -> set[str]:
    """Wort-Schlüssel eines Abos — aus dem Stichwort, sonst aus dem Namen."""
    return set(_merchant_key(sub.match_keyword or sub.name).split())


def _passt(sub_worte: set[str], tx_worte: set[str]) -> bool:
    """Gehören ein getippter Abo-Name und ein Banktext zum selben Händler?

    Auf Gleichheit der Schlüssel zu prüfen scheitert in der Praxis fast immer:
    das Abo heisst „Miete", die Buchung „Miete Wohnung"; das Abo „KI-Dienste",
    die Buchung „MUSTERDIENST KI VIA ZAHLDIENST". Beim Gruppieren von Buchungen
    untereinander (``detect_subscriptions``) genügt Gleichheit, weil dort beide
    Seiten aus derselben Quelle stammen — hier nicht.

    Ein gemeinsames bedeutungstragendes Wort reicht deshalb. Dass daraus keine
    zufälligen Treffer werden, sichert der Betragsvergleich beim Aufrufer.
    """
    return bool(sub_worte & tx_worte)


def _typischer_tag(daten: list[date]) -> int | None:
    """Der übliche Monatstag dieser Zahlung, aus der Historie.

    Median statt Durchschnitt: eine einzelne verschobene Zahlung (Feiertag,
    Wochenende) soll den Tag nicht verrücken. Unter drei Beobachtungen ist der
    Wert Rauschen — dann lieber gar keine Angabe als eine erfundene.
    """
    if len(daten) < 3:
        return None
    return int(statistics.median(d.day for d in daten))


def offene_fixabgaenge(
    db: Session, month_start: date, today: date | None = None
) -> dict:
    """Aktive Fixkosten und Abos, die in diesem Monat noch nicht gebucht sind.

    Gibt ``{"posten": [...], "summe": Decimal}`` zurück; ``posten`` ist absteigend
    nach Betrag sortiert und enthält je Eintrag ``name``, ``betrag``, ``kind``
    und ``tag`` (üblicher Monatstag oder ``None``).
    """
    heute = today or heute_lokal()
    monatsende = add_months(month_start, 1)
    if monatsende <= heute.replace(day=1):
        return {"posten": [], "summe": Decimal("0")}  # abgelaufener Monat

    subs = list(
        db.scalars(select(ManualSubscription).where(ManualSubscription.is_active.is_(True)))
    )
    if not subs:
        return {"posten": [], "summe": Decimal("0")}

    # Buchungen des Monats und die letzten 12 Monate Historie einmal laden. Die
    # Zuordnung läuft über Wortmengen, deshalb keine Gruppierung per Schlüssel.
    def _laden(von: date, bis: date) -> list[tuple[set[str], Decimal, date]]:
        return [
            (set(_merchant_key(tx.description).split()), tx.amount.copy_abs(), tx.date)
            for tx in db.scalars(
                select(Transaction).where(Transaction.date >= von, Transaction.date < bis)
            )
        ]

    gebucht = _laden(month_start, monatsende)
    historie = _laden(add_months(month_start, -12), month_start)

    posten = []
    summe = Decimal("0")
    for sub in subs:
        betrag = monthly_equivalent(sub.amount, sub.interval or BudgetInterval.MONATLICH)
        if betrag <= 0:
            continue
        worte = _schluessel(sub)
        if not worte:
            continue
        # Schon gebucht? Passender Händler UND ein Betrag in vertretbarer Nähe.
        # Der Betragsvergleich verhindert, dass etwa eine Migros-Rückerstattung
        # die Migros-Fixkosten als erledigt markiert.
        soll = sub.amount.copy_abs()
        toleranz = soll * _BETRAG_TOLERANZ
        if any(_passt(worte, w) and abs(b - soll) <= toleranz for w, b, _ in gebucht):
            continue
        tage = [d for w, b, d in historie if _passt(worte, w) and abs(b - soll) <= toleranz]
        posten.append({
            "name": sub.name,
            "betrag": betrag,
            "kind": sub.kind or "abo",
            "tag": _typischer_tag(tage),
        })
        summe += betrag

    posten.sort(key=lambda p: p["betrag"], reverse=True)
    return {"posten": posten, "summe": summe}
