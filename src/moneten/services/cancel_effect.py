"""Was bringt es, ein Abo zu kündigen?

Eine abstrakte Zahl („CHF 22 pro Monat") führt selten zu einer Entscheidung. Die
gleiche Zahl in Bezug auf ein konkretes Ziel („dein Sparziel wäre drei Wochen
früher voll") schon eher — derselbe Mechanismus, der den Treffen-Fonds
funktionieren lässt.

Bewusst KEINE Kündigungs-Automatik wie bei Rocket Money: die App verschickt
nichts und kennt keine Anbieter. Sie rechnet nur vor, was frei würde.

Zwei Dinge, die beim ersten Entwurf falsch waren und hier bewusst anders gelöst
sind:

* **„Früher" heisst Differenz, nicht Gesamtdauer.** Der erste Versuch rechnete
  aus, wie lange das ganze Ziel bei der freiwerdenden Rate dauern würde — das
  ergab bei einem 22-Franken-Abo und einem 4000-Franken-Ziel „789 Wochen früher".
  Richtig ist der Vergleich zweier Sparraten: heutige Rate gegen Rate plus
  freiwerdendem Betrag.
* **Nur echte Abos.** Für Fixkosten ergibt die Rechnung keinen Sinn — „Miete
  kündigen spart 17'400 im Jahr" ist kein Ratschlag.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import heute_lokal
from moneten.db.models import Account, BudgetInterval, ManualSubscription, SavingsGoal
from moneten.services.account_charts import account_balance_series
from moneten.services.median_budget import monthly_equivalent
from moneten.services.savings_alloc import _sort_key, allocate_savings

# Ein Monat hat im Schnitt 4.345 Wochen (365.25 / 12 / 7).
_WOCHEN_PRO_MONAT = Decimal("4.345")


def _sparrate(db: Session, konto: Account, today: date) -> Decimal:
    """Durchschnittliche Saldo-Zunahme pro Monat über sechs Monate.

    Dieselbe Definition wie in der Sparziel-Prognose — beide Zahlen müssen
    zueinander passen, sonst widerspricht sich die App auf zwei Seiten.
    """
    reihe = account_balance_series(db, konto, today, n=6)
    if len(reihe) < 2:
        return Decimal("0")
    return (reihe[-1] - reihe[0]) / (len(reihe) - 1)


def _naechstes_ziel(db: Session, zusatz: Decimal, heute: date) -> dict | None:
    """Das offene Sparziel, das dem Abschluss am nächsten ist, samt Zeitgewinn.

    Ohne verknüpftes Konto lässt sich keine Sparrate bestimmen; ohne Sparrate
    gibt es kein „früher" — dann steht dort ein ehrlicher Hinweis statt einer
    ausgedachten Zahl.
    """
    konten = {a.id: a for a in db.scalars(select(Account))}
    ziele = sorted(db.scalars(select(SavingsGoal)), key=_sort_key)
    # Verteilung wie auf der Sparziele-Seite: ein Konto, das zwei Ziele trägt,
    # zählt nicht doppelt.
    zuteilung = allocate_savings(
        ziele, {a.id: a.current_balance or Decimal("0") for a in konten.values()}
    )

    kandidaten = []
    for g in ziele:
        ziel_betrag = g.target_amount or Decimal("0")
        konto = konten.get(g.account_id) if g.account_id else None
        if g.is_achieved or ziel_betrag <= 0 or konto is None:
            continue
        rest = ziel_betrag - zuteilung.get(g.id, Decimal("0"))
        if rest <= 0:
            continue
        kandidaten.append((rest, g, konto))
    if not kandidaten:
        return None

    rest, g, konto = min(kandidaten, key=lambda k: k[0])
    rate = _sparrate(db, konto, heute)
    if rate <= 0:
        return {"name": g.name, "rest": rest, "wochen": None,
                "hinweis": "dort wächst der Saldo derzeit nicht"}

    monate_jetzt = rest / rate
    monate_danach = rest / (rate + zusatz)
    wochen = int((monate_jetzt - monate_danach) * _WOCHEN_PRO_MONAT)
    if wochen < 1:
        return {"name": g.name, "rest": rest, "wochen": None,
                "hinweis": "das macht dort weniger als eine Woche aus"}
    return {"name": g.name, "rest": rest, "wochen": wochen, "hinweis": None}


def kuendigungs_effekt(
    db: Session, sub: ManualSubscription, today: date | None = None
) -> dict | None:
    """Was ein gekündigtes Abo im Jahr frei macht — und wie viel früher damit das
    nächste Sparziel erreicht wäre.

    ``None`` für Fixkosten (kein Abo) und für Beträge von 0.
    """
    if (sub.kind or "abo") != "abo":
        return None
    monatlich = monthly_equivalent(sub.amount, sub.interval or BudgetInterval.MONATLICH)
    if monatlich <= 0:
        return None

    heute = today or heute_lokal()
    return {
        "monatlich": monatlich,
        "jaehrlich": (monatlich * 12).quantize(Decimal("0.01")),
        "ziel": _naechstes_ziel(db, monatlich, heute),
    }
