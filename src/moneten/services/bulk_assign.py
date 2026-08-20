"""Massen-Zuweisung: alle Treffer eines Buchungsfilters einer Kategorie zuordnen.

Der Anlass: 195 Buchungen ohne Kategorie, die heute einzeln angeklickt werden
müssen. Der Nutzer sucht „orell füssli" und will alle Treffer in einem Zug
zuordnen.

Drei Entscheidungen, die hier fest verdrahtet sind — jede gegen einen konkreten
Fehler:

1. **Der Filter ist die Wahrheit, nicht die Seite.** Die Buchungsliste lädt
   Monatskarten fensterweise nach (``before=``). Würde die Zuweisung über die
   geladenen Buchungen laufen, träfe sie nur die erste Seite — die Vorschau
   „195 Buchungen" und das Ergebnis „18 zugeordnet" lägen auseinander. Darum
   arbeiten Vorschau UND Zuweisung über dieselben WHERE-Bedingungen, ganz ohne
   Fenster und ohne Limit.
2. **Nichts überschreiben, was schon eine Kategorie hat.** Der Bestand sagt an
   mehreren Stellen „manuell gesetzte Kategorien werden nie überschrieben"
   (:func:`services.categorization.apply_rules` mit ``only_uncategorized``,
   :func:`learn_from_transaction`). Eine Massen-Zuweisung, die das still
   unterläuft, wäre der teuerste Bruch dieser Zusage. Umkategorisieren geht
   trotzdem — aber nur als bewusster, sichtbarer Extra-Schritt (``overwrite``).
3. **Aufgeteilte Buchungen und Umbuchungen bleiben aussen vor.** Bei einer
   aufgeteilten Buchung tragen die Splits die Kategorien; eine Eltern-Kategorie
   daneben ergäbe zwei widersprüchliche Zuordnungen. Eine Umbuchung ist weder
   Ausgabe noch Einnahme — sie zu kategorisieren würde sie aus der
   Transfer-Logik werfen. Gleiche Ausnahmen wie in ``services.categorization``.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.db.models import ManagementType, Transaction, not_transfer

# Obergrenze für die Rückgängig-Nutzlast. Der Vorzustand reist — wie bei den
# Regel-Aktionen (``routers/rules.py``) — im ``HX-Trigger``-HEADER zum Browser,
# und HTTP-Header sind serverseitig auf wenige KB begrenzt. Darüber wird zwar
# zugeordnet, aber ohne Undo-Angebot; die Sicherheitsabfrage sagt das vorher,
# statt hinterher einen abgeschnittenen Header zu produzieren.
BULK_UNDO_MAX = 800


def bulk_conditions(conds: list) -> list:
    """Filter-Bedingungen + die Ausnahmen, die eine Massen-Zuweisung nie anfasst.

    Siehe Modul-Docstring, Punkt 3.
    """
    return [*conds, Transaction.is_split.is_(False), not_transfer()]


def bulk_preview(db: Session, conds: list) -> dict:
    """Vorschau: Anzahl und Netto-Summe der betroffenen Buchungen, getrennt nach
    „noch ohne Kategorie" und „bereits zugeordnet".

    Zwei Aggregat-Queries über den **ganzen** Filter statt hydrierter Objekte —
    die Vorschau steht über einer Liste, die vielleicht nur sechs Monate geladen
    hat, und muss trotzdem über alle Treffer sprechen.

    Netto-Summe **mit** Vorzeichen, wie bei den Inbox-Gruppen: eine Auswahl mit
    +1'000 und −200 zeigte als Summe der Beträge sonst 1'200 — eine Zahl, die es
    nicht gibt.
    """
    base = bulk_conditions(conds)

    def _agg(extra) -> tuple[int, Decimal]:
        row = db.execute(
            select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(*base, extra)
        ).one()
        return int(row[0]), Decimal(str(row[1])).quantize(Decimal("0.01"))

    offen_count, offen_sum = _agg(Transaction.category_id.is_(None))
    belegt_count, belegt_sum = _agg(Transaction.category_id.is_not(None))
    return {
        "offen_count": offen_count, "offen_sum": offen_sum,
        "belegt_count": belegt_count, "belegt_sum": belegt_sum,
        "alle_count": offen_count + belegt_count, "alle_sum": offen_sum + belegt_sum,
    }


# ---------------------------------------------------------------------------
# Rückgängig: den Vorzustand als kompakte Zeichenkette mitführen
# ---------------------------------------------------------------------------
#
# Format:  "<kategorie>|<verwaltungsart>:<id>,<id>,…;<kategorie>|<art>:<id>,…"
#
# Gebündelt nach Vorzustand statt „id:zustand" je Buchung: bei einer typischen
# Zuweisung haben fast alle Buchungen denselben Vorzustand (gar keine Kategorie)
# und landen in EINEM Bündel — das halbiert die Nutzlast im Header. Gemischte
# Vorzustände (teils offen, teils bereits zugeordnet) ergeben mehrere Bündel und
# werden dadurch buchungsgenau wiederhergestellt, nicht pauschal auf „offen"
# gesetzt. Genau daran krankt das ältere Undo der Regel-Seite.


def pack_undo(rows: list[tuple[int, int | None, ManagementType | None]]) -> str:
    """Bündelt ``(tx_id, kategorie, verwaltungsart)`` zur Undo-Nutzlast."""
    buendel: dict[tuple[str, str], list[str]] = {}
    for tx_id, cat_id, mgmt in rows:
        key = (str(cat_id or ""), str(mgmt.value if mgmt is not None else ""))
        buendel.setdefault(key, []).append(str(tx_id))
    return ";".join(
        f"{cat}|{mgmt}:{','.join(ids)}" for (cat, mgmt), ids in buendel.items()
    )


def unpack_undo(payload: str) -> list[tuple[list[int], int | None, ManagementType | None]]:
    """Liest die Undo-Nutzlast zurück: ``[(ids, kategorie, verwaltungsart), …]``.

    Bewusst wegwerfend statt streng: kaputte Bündel werden übersprungen, damit
    ein verstümmelter Header nicht die ganze Rücknahme verhindert.
    """
    out: list[tuple[list[int], int | None, ManagementType | None]] = []
    for teil in payload.split(";"):
        if ":" not in teil:
            continue
        kopf, _, id_teil = teil.partition(":")
        cat_raw, _, mgmt_raw = kopf.partition("|")
        ids = [int(t) for t in id_teil.split(",") if t.strip().isdigit()]
        if not ids:
            continue
        cat_id = int(cat_raw) if cat_raw.strip().isdigit() else None
        try:
            mgmt = ManagementType(mgmt_raw) if mgmt_raw else None
        except ValueError:
            mgmt = None
        out.append((ids, cat_id, mgmt))
    return out
