"""Prognose + Stresstest (Phase 4).

* **12-Monats-Prognose:** lineare Extrapolation des Gesamtvermögens aus dem
  Trend der letzten Monate (``net_worth_series``). Bewusst simpel und
  nachvollziehbar — kein ML, nur „so geht's weiter, wenn der Schnitt bleibt".
* **Stresstest:** „Was passiert bei −15 % Einkommen / +20 % Ausgaben / einer
  einmaligen Ausgabe?" — rechnet den neuen Monatssaldo und die Reichweite
  (Runway) der liquiden Mittel aus.

Alles aus den vorhandenen Buchungen, offline, serverseitig.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import Transaction, not_transfer
from moneten.services.account_charts import net_worth_series
from moneten.templating import MONATE


def monthly_in_out(db: Session, today: date, lookback: int = 6) -> tuple[Decimal, Decimal]:
    """Durchschnittliche **monatliche** Einnahmen und Ausgaben (positiv) über die
    letzten ``lookback`` abgeschlossenen Monate (ohne den laufenden, da unvollständig).

    Gibt ``(einnahmen_pro_monat, ausgaben_pro_monat)`` zurück.
    """
    start = add_months(today.replace(day=1), -lookback)
    end = today.replace(day=1)  # exklusiv: laufender Monat zählt nicht (Teilmonat)
    # Divisor an die real vorhandene Historie anpassen: wer erst seit 2 Monaten
    # Daten hat, bekäme sonst Summe/6 → viel zu tiefe Monatswerte in der Prognose.
    first_tx = db.scalar(select(func.min(Transaction.date)).where(not_transfer()))
    if first_tx is None:
        return Decimal("0.00"), Decimal("0.00")
    start = max(start, first_tx.replace(day=1))
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if months < 1:
        return Decimal("0.00"), Decimal("0.00")
    income = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.amount > 0, Transaction.date >= start, Transaction.date < end, not_transfer()
        )
    ) or 0
    expense = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.amount < 0, Transaction.date >= start, Transaction.date < end, not_transfer()
        )
    ) or 0
    n = Decimal(months)
    avg_income = (Decimal(str(income)) / n).quantize(Decimal("0.01"))
    avg_expense = (Decimal(str(expense)).copy_abs() / n).quantize(Decimal("0.01"))
    return avg_income, avg_expense


@dataclass
class Projection:
    points_hist: str       # SVG-Polyline-Punkte der bisherigen Werte
    points_fc: str         # SVG-Polyline-Punkte der Prognose
    labels: list[str]      # Monats-Labels (gekürzt) für die x-Achse
    end_value: Decimal     # prognostiziertes Vermögen am Ende des Horizonts
    monthly_change: Decimal
    width: int
    height: int
    # Geometrie + Stützpunkte, damit der Stresstest die Linie live im selben
    # Koordinatensystem neu zeichnen kann (Client-seitig) und der Hover funktioniert.
    pad: int
    lo: Decimal
    span: Decimal
    hist_len: int
    horizon: int
    last_value: Decimal
    points: list[dict]     # [{x, y, label, value}] für alle Stützpunkte (Hover)


def net_worth_projection(db: Session, today: date, horizon: int = 12, history: int = 6) -> Projection:
    """Extrapoliert das Gesamtvermögen ``horizon`` Monate in die Zukunft.

    Steigung = durchschnittliche monatliche Veränderung der **abgeschlossenen**
    Monate (siehe unten). Liefert fertige SVG-Polyline-Punkte für ein offline
    gezeichnetes Liniendiagramm (Historie durchgezogen, Prognose gestrichelt).
    """
    hist = net_worth_series(db, today, n=history)  # [{month, value}], alt→neu
    hist_vals = [h["value"] for h in hist] or [Decimal("0")]

    # **Die Steigung kommt nur aus ABGESCHLOSSENEN Monaten.** Der letzte Punkt
    # der Reihe ist der laufende Monat, und dem fehlt, was noch kommt: solange
    # der Lohn dieses Monats nicht gebucht ist, liegt er unter seinen Vormonaten.
    #
    # Vorher lief die Steigung über genau diesen Punkt. Gemessen an sieben
    # Monaten mit je +1000 ergab das eine gemeldete Veränderung von **−200 pro
    # Monat**, weil im laufenden Monat 5000 Lohn fehlten: die Prognoselinie fiel,
    # während das Vermögen stieg — nach zwölf Monaten 14'400 daneben, mit
    # falschem Vorzeichen. Das ist die Sorte Fehler, die niemandem auffällt,
    # weil eine fallende Linie plausibel aussieht.
    #
    # Der Startpunkt bleibt der echte heutige Wert; er ist eine Tatsache, keine
    # Schätzung. Dadurch liegt die Linie im laufenden Monat eher zu tief als zu
    # hoch — bei einer Vermögensprognose die vorsichtigere Richtung.
    abgeschlossen = hist_vals[:-1] if len(hist_vals) > 2 else hist_vals
    slope = ((abgeschlossen[-1] - abgeschlossen[0]) / (len(abgeschlossen) - 1)
             if len(abgeschlossen) > 1 else Decimal("0"))

    last_month = hist[-1]["month"] if hist else today.replace(day=1)
    fc_vals = [hist_vals[-1] + slope * i for i in range(1, horizon + 1)]
    all_vals = hist_vals + fc_vals

    width, height, pad = 560, 150, 8
    lo, hi = min(all_vals), max(all_vals)
    span = (hi - lo) or Decimal("1")
    n_total = len(all_vals)

    def _xy(idx: int, val: Decimal) -> tuple[float, float]:
        x = pad + (width - 2 * pad) * (idx / max(n_total - 1, 1))
        y = (height - pad) - (height - 2 * pad) * float((val - lo) / span)
        return round(x, 1), round(y, 1)

    pts_hist = " ".join(f"{x},{y}" for i, v in enumerate(hist_vals) for x, y in [_xy(i, v)])
    # Prognose-Linie beginnt am letzten Historien-Punkt (nahtloser Übergang).
    pts_fc = " ".join(
        f"{x},{y}" for j, v in enumerate([hist_vals[-1], *fc_vals]) for x, y in [_xy(len(hist_vals) - 1 + j, v)]
    )
    month_objs = [h["month"] for h in hist] + [add_months(last_month, i) for i in range(1, horizon + 1)]
    labels = [MONATE[m.month - 1][:3] for m in month_objs]
    # Stützpunkte für den Hover-Tooltip (Monat + Vermögen je Punkt).
    points = []
    for i, (m, v) in enumerate(zip(month_objs, all_vals, strict=False)):
        px, py = _xy(i, v)
        points.append({"x": px, "y": py, "label": f"{MONATE[m.month - 1]} {m.year}", "value": v})

    return Projection(
        points_hist=pts_hist,
        points_fc=pts_fc,
        labels=labels,
        end_value=fc_vals[-1].quantize(Decimal("0.01")),
        monthly_change=slope.quantize(Decimal("0.01")),
        width=width,
        height=height,
        pad=pad,
        lo=lo,
        span=span,
        hist_len=len(hist_vals),
        horizon=horizon,
        last_value=hist_vals[-1],
        points=points,
    )


@dataclass
class StressResult:
    new_income: Decimal
    new_expense: Decimal
    new_saldo: Decimal       # pro Monat
    base_saldo: Decimal
    runway_months: float | None  # None = unbegrenzt (Saldo ≥ 0)


def stresstest(
    *, base_income: Decimal, base_expense: Decimal, income_pct: int, expense_pct: int,
    one_time: Decimal, liquid: Decimal,
) -> StressResult:
    """Rechnet ein Szenario durch.

    * ``income_pct`` / ``expense_pct``: prozentuale Änderung (z.B. -15 / +20).
    * ``one_time``: einmalige Sonderausgabe (reduziert sofort die liquiden Mittel).
    * ``liquid``: aktuell liquide Mittel (Notgroschen), gegen die der Runway läuft.

    Runway = wie viele Monate die liquiden Mittel den (negativen) Monatssaldo
    decken. Bei nicht-negativem Saldo: unbegrenzt (None).
    """
    new_income = (base_income * (100 + income_pct) / 100).quantize(Decimal("0.01"))
    new_expense = (base_expense * (100 + expense_pct) / 100).quantize(Decimal("0.01"))
    saldo = (new_income - new_expense).quantize(Decimal("0.01"))
    available = liquid - one_time
    if available < 0:
        # Die Einmalausgabe übersteigt die liquiden Mittel bereits — dann hilft
        # auch ein positiver Monatssaldo nicht, das Geld fehlt sofort. Vorher
        # meldete dieser Fall „unbegrenzt", weil nur das Vorzeichen des Saldos
        # geprüft wurde und `one_time` unter den Tisch fiel.
        runway = 0.0
    elif saldo >= 0:
        runway = None
    else:
        runway = round(float(available / saldo.copy_abs()), 1) if available > 0 else 0.0
    return StressResult(
        new_income=new_income,
        new_expense=new_expense,
        new_saldo=saldo,
        base_saldo=(base_income - base_expense).quantize(Decimal("0.01")),
        runway_months=runway,
    )
