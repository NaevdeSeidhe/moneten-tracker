"""Kleine Datums-Helfer, die an mehreren Stellen gebraucht werden.

Zentral, damit die Monats-Arithmetik nur **einmal** existiert (vorher war sie in
Budget-, Dashboard- und mehreren Service-Dateien dupliziert).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: Vorgabe, wenn nichts konfiguriert ist.
_TZ_VORGABE = "Europe/Zurich"

#: Der Rückfall auf die Systemzeit wird EINMAL gemeldet, nicht bei jedem Aufruf.
_RUECKFALL_GEMELDET = False


def _zeitzone() -> str:
    """Die konfigurierte Zeitzone (``MONETEN_TIMEZONE``).

    Absichtlich eine Funktion und keine Konstante: so bleibt dieses Modul frei
    von einem Import auf ``config`` zur Ladezeit, und wer die Einstellung ändert,
    braucht keinen Neustart der Importkette.
    """
    try:
        from moneten.config import settings

        return settings.timezone or _TZ_VORGABE
    except Exception:
        return _TZ_VORGABE


def local_now() -> datetime:
    """Aktuelle Zeit in der Zeitzone der App.

    Fällt robust auf die System-Zeit zurück, falls die IANA-Zeitzonendaten fehlen
    (z.B. ohne ``tzdata`` lokal auf Windows) — dort ist die System-Zeit ohnehin
    bereits die richtige Lokalzeit.
    """
    try:
        return datetime.now(ZoneInfo(_zeitzone()))
    except Exception:
        return datetime.now()


def heute_lokal() -> date:
    """Das heutige Datum in der Zeitzone der App — anstelle von ``date.today()``.

    **Warum es diese Funktion braucht.** Der Container läuft in UTC, und
    ``date.today()`` liefert dort das UTC-Datum. Zwischen Mitternacht und 02:00
    Zürcher Zeit ist das der VORTAG. Nachgerechnet:  um 00:30 zeigten
    Budget, Dashboard und Schnellerfassung noch den August;  um
    00:45 nannte die Steuerseite das Jahr 2025 — samt verschobener Jahresgrenze
    in der Jahresprobe des Lohns.

    Der Fehler hatte alles, was ihn unauffällig macht: nur nachts, nur in einem
    Zweistundenfenster, und die Zahlen waren in sich stimmig — bloss für den
    falschen Monat. ``local_now`` gab es schon; benutzt wurde es an genau einer
    Stelle, für die Begrüssung.

    Der Name ist bewusst nicht bloss ``heute``: so heisst in diesem Projekt an
    einem guten Dutzend Stellen eine lokale Variable oder ein Parameter. Ein
    Import gleichen Namens würde sie überdecken — beim maschinellen Umstellen
    ist mir genau das passiert, mit drei stillen Namenskollisionen.
    """
    return local_now().date()


def add_months(d: date, delta: int) -> date:
    """Verschiebt ein Datum um ``delta`` Monate und gibt den **ersten Tag** des
    Zielmonats zurück.

    Rechnet über eine fortlaufende Monatszahl (Jahr*12 + Monat), damit der
    Jahreswechsel automatisch stimmt. ``delta`` darf negativ sein (Vormonate).
    Der Tag der Eingabe wird ignoriert — Ergebnis ist immer der 1. des Monats.

    Beispiel: ``add_months(date(2026, 1, 31), -1) == date(2025, 12, 1)``.
    """
    total = (d.year * 12 + (d.month - 1)) + delta
    return date(total // 12, total % 12 + 1, 1)
