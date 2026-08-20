"""Bar gegen digital — wie viel des Alltags wird mit Bargeld bezahlt.

Hintergrund: Bargeld fühlt sich beim Ausgeben anders an als Karte oder TWINT
(„pain of paying"), digital ist aber schneller — man rutscht ohne Absicht ins
Digitale. Diese Auswertung macht den Stand sichtbar, statt ihn zu erraten.

DREI ENTSCHEIDUNGEN, die das Ergebnis prägen — alle bewusst getroffen:

1. **Der Nenner sind nur Alltagsausgaben.** Miete, Krankenkasse und Abos kann
   man nicht bar zahlen. Nimmt man sie mit hinein, sinkt die Quote strukturell
   auf wenige Prozent und misst nur noch, wie hoch die Fixkosten sind — nicht,
   wie oft man an der Kasse zum Bargeld greift. Ausgeschlossen sind darum
   Daueraufträge, Rückstellungen, Sparen, Einkommen und Umbuchungen.

2. **Bargeldbezüge zählen nicht als Ausgabe.** Sie sind Umbuchungen zwischen
   Konto und Kassette; ausgegeben ist das Geld erst danach. Gezählt wird also
   *bezahlt*, nicht *abgehoben*.

3. **Vergessenes Bargeld zählt mit — aber sichtbar getrennt.** Barzahlungen
   landen nur in der App, wenn man sie erfasst; Kartenzahlungen kommen
   automatisch aus dem CAMT-Import. Ohne Gegenmassnahme würde die Quote
   Vergesslichkeit als mangelnde Bargeld-Disziplin ausweisen. Genau diese Lücke
   deckt der Kassensturz auf: Fehlt beim Zählen Geld, war es Bargeld-Ausgabe,
   die nie erfasst wurde. Diese Korrekturen fliessen darum in die Bar-Seite ein,
   werden aber separat ausgewiesen — eine hohe Zahl dort heisst „erfasse
   sorgfältiger", nicht „zahle mehr bar".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import Account, AccountType, Category, ManagementType, Transaction
from moneten.templating import MONATE

# Beschreibungs-Präfix der Kassensturz-Korrektur. Bewusst hier als Konstante,
# damit Buchen (routers/accounts.py) und Auswerten dieselbe Quelle nutzen und
# nicht auseinanderlaufen können.
KASSENSTURZ_PREFIX = "Kassensturz-Korrektur"

# Verwaltungsarten, die keine Kaufentscheidung an der Kasse sind (siehe Punkt 1).
_NICHT_ALLTAG = {
    ManagementType.DAUERAUFTRAG,
    ManagementType.RUECKSTELLUNG,
    ManagementType.SPAREN,
    ManagementType.EINKOMMEN,
    ManagementType.TRANSFER,
}


def _ist_alltag(mt: ManagementType | None) -> bool:
    """Ohne Verwaltungsart gilt eine Ausgabe als Alltag — eine unkategorisierte
    Buchung ist typischerweise ein Einkauf, kein Dauerauftrag."""
    return mt not in _NICHT_ALLTAG


def payment_mix(db: Session, today: date, monate: int = 12, ziel_pct: int = 0) -> dict:
    """Bar-/Digital-Anteil der Alltagsausgaben je Monat.

    :param monate: Länge des Rückblicks inklusive des laufenden Monats.
    :param ziel_pct: gewünschter Bar-Anteil in Prozent (0 = kein Ziel gesetzt).
    """
    aktueller_monat = today.replace(day=1)
    start = add_months(aktueller_monat, -(monate - 1))

    zeilen = db.execute(
        select(
            Transaction.date,
            Transaction.amount,
            Transaction.management_type,
            Transaction.description,
            Transaction.category_id,
            Account.type,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.amount < 0, Transaction.date >= start)
    ).all()

    leer = {"bar": Decimal("0"), "bar_unerfasst": Decimal("0"), "digital": Decimal("0")}
    eimer: dict[date, dict[str, Decimal]] = {}
    m = start
    while m <= aktueller_monat:
        eimer[m] = dict(leer)
        m = add_months(m, 1)

    # Kategorie-Aufschlüsselung nur für den Rückblick-Zeitraum insgesamt.
    je_kategorie: dict[int | None, dict[str, Decimal]] = {}

    for d, betrag, mt, beschreibung, cat_id, konto_typ in zeilen:
        if not _ist_alltag(mt):
            continue
        monat = d.replace(day=1)
        if monat not in eimer:
            continue
        wert = -betrag  # Ausgaben sind negativ gespeichert
        if konto_typ == AccountType.CASH:
            schluessel = (
                "bar_unerfasst"
                if (beschreibung or "").startswith(KASSENSTURZ_PREFIX)
                else "bar"
            )
        else:
            schluessel = "digital"
        eimer[monat][schluessel] += wert
        k = je_kategorie.setdefault(cat_id, dict(leer))
        k[schluessel] += wert

    monatsreihe = []
    for monat in sorted(eimer):
        e = eimer[monat]
        bar_total = e["bar"] + e["bar_unerfasst"]
        summe = bar_total + e["digital"]
        monatsreihe.append({
            "month": monat,
            "label": MONATE[monat.month - 1][:3],
            "jahr": monat.year,
            "bar": e["bar"],
            "bar_unerfasst": e["bar_unerfasst"],
            "bar_total": bar_total,
            "digital": e["digital"],
            "summe": summe,
            # Anteile in Prozent der Monatssumme — die Balken sind auf 100 %
            # normiert, damit sich Monate mit unterschiedlichem Ausgabenniveau
            # überhaupt vergleichen lassen.
            "pct": int(round(float(bar_total / summe * 100))) if summe > 0 else None,
            "pct_bar": float(e["bar"] / summe * 100) if summe > 0 else 0.0,
            "pct_unerfasst": float(e["bar_unerfasst"] / summe * 100) if summe > 0 else 0.0,
        })

    aktuell = monatsreihe[-1] if monatsreihe else None
    mit_werten = [r for r in monatsreihe if r["pct"] is not None]
    schnitt = (
        int(round(sum(r["pct"] for r in mit_werten) / len(mit_werten))) if mit_werten else None
    )

    # Kategorien: wo zahlst du bar, wo digital? Nur Kategorien mit Bewegung.
    cat_map = {c.id: c for c in db.scalars(select(Category))}
    kategorien = []
    for cat_id, w in je_kategorie.items():
        bar_total = w["bar"] + w["bar_unerfasst"]
        summe = bar_total + w["digital"]
        if summe <= 0:
            continue
        cat = cat_map.get(cat_id) if cat_id else None
        kategorien.append({
            "name": cat.name if cat else "Ohne Kategorie",
            "icon": cat.icon if cat else "tag",
            "bar": bar_total,
            "digital": w["digital"],
            "summe": summe,
            "pct": int(round(float(bar_total / summe * 100))),
        })
    kategorien.sort(key=lambda k: k["summe"], reverse=True)

    unerfasst_gesamt = sum((r["bar_unerfasst"] for r in monatsreihe), Decimal("0"))

    return {
        "monate": monatsreihe,
        "aktuell": aktuell,
        "schnitt_pct": schnitt,
        "ziel_pct": ziel_pct,
        # Nur die grössten Kategorien — die lange Liste hilft beim Entscheiden nicht.
        "kategorien": kategorien[:8],
        "unerfasst_gesamt": unerfasst_gesamt,
        "hat_daten": any(r["summe"] > 0 for r in monatsreihe),
    }


def _faellig_regel(letzter: date | None, today: date) -> bool:
    """Die reine Entscheidung, ohne Datenbank — dadurch deterministisch prüfbar.

    Fällig, sobald im laufenden Kalendermonat noch nicht gezählt wurde. Aber
    nicht, wenn der letzte Kassensturz weniger als sieben Tage her ist: sonst
    stünde die Erinnerung am 1. schon wieder da, wenn man am 30. gezählt hat —
    und eine Erinnerung, die man regelmässig grundlos sieht, wirkt nicht mehr.
    """
    if letzter is None:
        return True
    return letzter < today.replace(day=1) and (today - letzter).days >= 7


def kassensturz_faellig(db: Session, today: date) -> dict:
    """Ist ein Kassensturz fällig? Monatsrhythmus, aber ohne Nörgeln.

    Fällig, sobald im laufenden Kalendermonat noch keiner gemacht wurde — das
    ergibt den gewünschten Rhythmus „zu Monatsbeginn den Vormonat abschliessen".

    ABER: nicht, wenn der letzte weniger als sieben Tage her ist. Sonst würde
    ein Kassensturz am 30. schon am 1. wieder eingefordert, und eine Erinnerung,
    die man regelmässig grundlos sieht, hört man nach zwei Monaten nicht mehr.

    Ohne Bargeld-Konto gibt es nichts zu zählen — dann nie fällig.
    """
    hat_kasse = db.scalar(
        select(Account.id).where(Account.type == AccountType.CASH, Account.is_active.is_(True))
    )
    if hat_kasse is None:
        return {"faellig": False, "letzter": None, "tage_her": None}

    letzter = db.scalar(
        select(Transaction.date)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Account.type == AccountType.CASH,
            Transaction.description.like(f"{KASSENSTURZ_PREFIX}%"),
        )
        .order_by(Transaction.date.desc())
        .limit(1)
    )
    if letzter is None:
        return {"faellig": True, "letzter": None, "tage_her": None}

    return {
        "faellig": _faellig_regel(letzter, today),
        "letzter": letzter,
        "tage_her": (today - letzter).days,
    }
