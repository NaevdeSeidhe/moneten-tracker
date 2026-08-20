"""Konten-Auswertung: Vermögens-Verlauf, Konto-Saldo-Reihen, Gruppierung.

Historische Salden werden **rückwärts** aus dem aktuellen Saldo gerechnet:
Saldo am Monatsende = aktueller Saldo − Summe aller späteren Buchungen. So ist
das rechte Ende der Kurve exakt der angezeigte Ist-Saldo (konsistent zum
Dashboard), ohne Saldo-Snapshots speichern zu müssen.

Nur Top-Level-Buchungen zählen (analog ``balances.recalc_account_balance``);
Transfers heben sich beim Gesamtvermögen gegenseitig auf.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.db.models import Account, AccountType, Transaction
from moneten.palette import color_at

# Gruppierung der Konto-Typen für die Konten-Seite.
GROUP_DEFS: list[tuple[str, set]] = [
    ("Liquide", {AccountType.BANK, AccountType.CASH}),
    ("Sparen", {AccountType.SAVINGS}),
    ("Anlage", {AccountType.INVESTMENT, AccountType.CRYPTO, AccountType.STOCKS}),
]


def _month_start(today: date, back: int) -> date:
    total = today.year * 12 + (today.month - 1) - back
    return date(total // 12, total % 12 + 1, 1)


def _sum_after(
    db: Session,
    end_exclusive: date,
    account_id: int | None = None,
    konten: list[int] | None = None,
) -> Decimal:
    """Summe der Buchungen ab ``end_exclusive`` (inkl.), optional je Konto.

    ``konten`` grenzt zusätzlich auf eine Kontenmenge ein — nötig, damit die
    Rückrechnung dieselben Konten betrachtet wie der Startsaldo.
    """
    q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.date >= end_exclusive,
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if konten is not None:
        q = q.where(Transaction.account_id.in_(konten))
    return Decimal(str(db.scalar(q) or 0))


def net_worth_series(db: Session, today: date, n: int = 12) -> list[dict]:
    """Gesamtvermögen am Ende der letzten ``n`` Monate (älteste zuerst).

    Nur **aktive** Konten. Die Konten-Seite rendert den letzten Punkt dieser
    Reihe als Leitzahl „Gesamtvermögen" und darunter Gruppen-Zwischensummen, die
    ihrerseits nach ``is_active`` filtern. Ohne denselben Filter hier ergäben die
    Zwischensummen nicht die Zahl über ihnen, und ein archiviertes Konto mit
    Saldo hätte das Vermögen still zu hoch ausgewiesen.

    Der Filter muss auch für die Rückrechnung gelten: würden dort die Buchungen
    archivierter Konten mitzählen, driftete die Kurve in die Vergangenheit weg.
    """
    accounts = db.scalars(select(Account).where(Account.is_active.is_(True))).all()
    ids = [a.id for a in accounts]
    now_total = sum((a.current_balance or Decimal("0") for a in accounts), Decimal("0"))
    out: list[dict] = []
    for back in range(n - 1, -1, -1):
        end = _month_start(today, back - 1)  # erster Tag des Folgemonats = exklusives Ende
        out.append({
            "month": _month_start(today, back),
            "value": now_total - _sum_after(db, end, konten=ids),
        })
    return out


def account_balance_series(db: Session, account: Account, today: date, n: int = 6) -> list[Decimal]:
    """Saldo eines Kontos am Ende der letzten ``n`` Monate (älteste zuerst)."""
    now = account.current_balance or Decimal("0")
    out: list[Decimal] = []
    for back in range(n - 1, -1, -1):
        end = _month_start(today, back - 1)
        out.append(now - _sum_after(db, end, account.id))
    return out


# Mehr als sechs Linien trägt ein Diagramm dieser Grösse nicht: ab da rät man
# beim Zuordnen Farbe→Konto mehr, als man abliest. Alles darüber wird zu EINER
# Sammelreihe addiert statt weggelassen — dadurch ergeben die gezeichneten
# Linien in jedem Monat zusammen weiterhin die Gesamtlinie.
MAX_KONTO_LINIEN = 6


def konto_verlaeufe(
    db: Session,
    today: date,
    n: int = 12,
    *,
    hoechstens: int = MAX_KONTO_LINIEN,
) -> list[dict]:
    """Einzelne Konto-Verläufe für den Vermögens-Verlauf (älteste zuerst).

    Nur **aktive** Konten. ``net_worth_series`` zählt ebenfalls nur diese; ein
    archiviertes Konto ergäbe eine Linie, die in der Gesamtlinie über ihr gar
    nicht steckt. Mit demselben Filter gilt: die Summe aller Reihen hier ist in
    jedem Monat genau der Wert der Gesamtlinie.

    Konten, die über den ganzen Zeitraum 0 sind, fallen raus — eine Linie auf
    der Nulllinie trägt keine Information und legt sich über die Achse.

    Sortiert nach dem grössten Betrag **im Zeitraum**, nicht nach dem heutigen
    Saldo: ein Konto, das im Januar 20'000 trug und heute leer ist, prägt das
    Bild. Nach heutigem Saldo stünde es zuhinterst und fiele als erstes der
    Zusammenfassung zum Opfer.

    Je Reihe: ``name`` (Legendentext), ``titel`` (Tooltip — bei der Sammelreihe
    die enthaltenen Konten), ``werte`` und ``rest``.
    """
    reihen: list[dict] = []
    for konto in db.scalars(select(Account).where(Account.is_active.is_(True))):
        werte = account_balance_series(db, konto, today, n)
        if all(w == 0 for w in werte):
            continue
        reihen.append({"name": konto.name, "titel": konto.name,
                       "werte": werte, "rest": False})

    reihen.sort(key=lambda r: max(w.copy_abs() for w in r["werte"]), reverse=True)

    if len(reihen) > hoechstens:
        uebrige = reihen[hoechstens - 1:]
        reihen = reihen[: hoechstens - 1]
        summe = [
            sum((r["werte"][i] for r in uebrige), Decimal("0"))
            for i in range(len(uebrige[0]["werte"]))
        ]
        if any(w != 0 for w in summe):
            reihen.append({
                "name": f"Übrige ({len(uebrige)})",
                "titel": " · ".join(r["name"] for r in uebrige),
                "werte": summe,
                "rest": True,
            })
    return reihen


# Die Gesamtlinie des Vermögens-Verlaufs ist ``--accent-primary``. In vier der
# sechs Skins (dark, nord, synthwave, melange) ist ``--chart-0`` Zeichen für
# Zeichen DIESELBE Farbe, in ``ayu-hell`` und ``light`` liegt sie mit dE 16.5
# bzw. 17.1 dicht daneben. Das erste Konto bekam damit die Farbe der
# Gesamtlinie — der Hauptgrund, warum sich im Bild keine Linie mehr zuordnen
# liess. ``--chart-0`` gehört deshalb der Gesamtlinie; die Konten fangen eins
# weiter an. Acht Palettenfarben minus die reservierte reichen für die
# höchstens sechs Konto-Linien.
FARB_VERSATZ = 1


def konto_farbe(index: int, *, rest: bool = False) -> str:
    """CSS-Variable für die Linie der ``index``-ten Konto-Reihe.

    Die Sammelreihe bleibt unbunt: sie ist ein Rest, kein Konto, und verbraucht
    so keine Palettenfarbe, die einem echten Konto zusteht. ``--text-tertiary``
    ist in jedem Skin auf mindestens 4.5:1 gegen die Karte gemessen
    (tests/test_skins.py) — als Linie also mehr als ausreichend.
    """
    if rest:
        return "var(--text-tertiary)"
    return color_at(index + FARB_VERSATZ)


def last_activity(db: Session, account_id: int) -> date | None:
    """Datum der jüngsten Buchung eines Kontos (oder None)."""
    return db.scalar(
        select(func.max(Transaction.date)).where(
            Transaction.account_id == account_id,
        )
    )
