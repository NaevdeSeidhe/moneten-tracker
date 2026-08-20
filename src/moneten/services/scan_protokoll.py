"""Das Scan-Protokoll füllen und lesen.

Ein eigener Dienst und keine drei Zeilen in der Route: geschrieben wird an einer
Stelle, aufgeräumt auch — sonst wächst das Protokoll bei jemandem, der viel
scannt, still weiter.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import ScanProtokoll

# So viele Scans bleiben stehen. Genug, um einen Fehler von gestern noch zu
# finden; wenig genug, dass die Datenbank nicht zum Beleg-Archiv wird.
MAX_EINTRAEGE = 25

# Länger als das wird kein Rohtext gespeichert. Ein Kassenbon hat selten mehr;
# was darüber liegt, ist meist ein mehrseitiges PDF, dessen Text ohnehin nicht
# aus der Erkennung stammt.
MAX_ZEICHEN = 20_000


def _dezimal(roh) -> Decimal | None:
    try:
        wert = Decimal(str(roh))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return wert if wert.is_finite() else None


def protokolliere(db: Session, *, quittung: dict, ocr_text: str, methode: str) -> None:
    """Hält einen Scan fest — und räumt die ältesten weg.

    Fehler hier dürfen den Scan NICHT scheitern lassen: das Protokoll ist ein
    Hilfsmittel, kein Teil des Vorgangs. Ein voller Datenträger oder ein
    unerwarteter Wert kostet sonst den ganzen Beleg.
    """
    try:
        db.add(ScanProtokoll(
            haendler=(quittung.get("merchant") or None),
            betrag=_dezimal(quittung.get("amount")),
            beleg_datum=None,
            methode=methode or "",
            positionen=len(quittung.get("items") or []),
            ocr_text=(ocr_text or "")[:MAX_ZEICHEN],
        ))
        db.flush()
        alt = list(db.scalars(
            select(ScanProtokoll).order_by(ScanProtokoll.created_at.desc()).offset(MAX_EINTRAEGE)
        ))
        for eintrag in alt:
            db.delete(eintrag)
        db.commit()
    except Exception:  # noqa: BLE001 — das Protokoll darf den Scan nie kippen
        db.rollback()


def letzte(db: Session, grenze: int = MAX_EINTRAEGE) -> list[ScanProtokoll]:
    """Die jüngsten Scans, neueste zuerst."""
    return list(db.scalars(
        select(ScanProtokoll).order_by(ScanProtokoll.created_at.desc()).limit(grenze)
    ))
