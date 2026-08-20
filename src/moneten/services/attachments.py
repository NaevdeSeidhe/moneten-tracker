"""Quittungs-Anbindung per Ordner-Referenz (kein Datei-Upload).

Die Quittungen liegen oft schon in einem Ordner (z.B. einer Scanner-Ablage). Die App
**kopiert nichts** — sie merkt sich nur den Dateinamen bzw. den Pfad innerhalb
des konfigurierten Quittungs-Ordners (``MONETEN_RECEIPTS_DIR``). So gibt es keine
Duplikate, und das Original bleibt unter eigener Kontrolle.

In der DB hält die Tabelle ``attachments``:
* ``original_name`` — der Dateiname (immer gesetzt)
* ``file_path``     — der volle Pfad im Quittungs-Ordner (zum Anzeigen, optional)

Sicherheit: Beim Ausliefern wird geprüft, dass der Pfad **innerhalb** des
konfigurierten Ordners liegt (kein Pfad-Traversal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from moneten.config import settings

# Datei-Endungen, die als Quittung gelten.
RECEIPT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".heic"}

# Datums-Muster in Dateinamen (für die Auto-Zuordnung).
_DATE_PATTERNS = [
    (re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})"), ("y", "m", "d")),   # 2026-05-15 / 2026_05_15
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), ("y", "m", "d")), # 20260515
    (re.compile(r"(\d{2})[-_.](\d{2})[-_.](\d{4})"), ("d", "m", "y")),   # 15.05.2026
    (re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)"), ("d", "m", "y")), # 15052026 / 07012024 (TTMMJJJJ)
]


@dataclass
class ReceiptFile:
    """Eine Datei im Quittungs-Ordner."""

    name: str          # Dateiname
    path: str          # voller Pfad
    parsed_date: date | None  # aus dem Dateinamen erkanntes Datum (falls vorhanden)
    size: int          # Bytes


def receipts_dir() -> Path | None:
    """Konfigurierter Quittungs-Ordner als Path, oder None wenn nicht gesetzt/vorhanden."""
    if settings.receipts_dir is None:
        return None
    p = Path(settings.receipts_dir)
    return p if p.is_dir() else None


def parse_date_from_name(filename: str) -> date | None:
    """Versucht, ein Datum aus dem Dateinamen zu lesen (gängige Schreibweisen)."""
    for pattern, order in _DATE_PATTERNS:
        m = pattern.search(filename)
        if not m:
            continue
        parts = dict(zip(order, m.groups(), strict=True))
        try:
            return date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
        except ValueError:
            continue
    return None


def list_receipts() -> list[ReceiptFile]:
    """Listet alle Quittungs-Dateien im konfigurierten Ordner (nicht rekursiv).

    Leere Liste, wenn kein Ordner konfiguriert ist.
    """
    base = receipts_dir()
    if base is None:
        return []
    result: list[ReceiptFile] = []
    for entry in sorted(base.iterdir()):
        if entry.is_file() and entry.suffix.lower() in RECEIPT_EXTENSIONS:
            result.append(
                ReceiptFile(
                    name=entry.name,
                    path=str(entry),
                    parsed_date=parse_date_from_name(entry.name),
                    size=entry.stat().st_size,
                )
            )
    return result


def resolve_receipt(file_path: str) -> Path | None:
    """Validiert, dass ``file_path`` innerhalb des Quittungs-Ordners liegt und existiert.

    Schützt gegen Pfad-Traversal: nur Dateien unterhalb des konfigurierten
    Ordners dürfen ausgeliefert werden.
    """
    base = receipts_dir()
    if base is None:
        return None
    try:
        target = Path(file_path).resolve()
        base_resolved = base.resolve()
        target.relative_to(base_resolved)  # wirft ValueError wenn ausserhalb
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None
