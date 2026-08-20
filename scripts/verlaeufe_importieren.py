"""Importiert eine ``verlaeufe.json`` — ohne Browser, ohne Login.

Läuft **im Container**, wo der Datenbank-Schlüssel schon in der Umgebung steht.
Genau darum gibt es dieses Skript: der Weg über das Formular verlangt einen
Login, und ein Schritt, der einen Login verlangt, wird beim Deploy vergessen.
Der Import ist der zweite Halbschritt zur Extraktion — einer ohne den anderen
hinterlässt eine Datei, die niemand ansieht.

Aufruf (so läuft es beim Deploy)::

    docker exec moneten python scripts/verlaeufe_importieren.py /app/data/verlaeufe.json

Dieselben Regeln wie im Formular: geschrieben wird über
:func:`moneten.routers.metrics.importiere_befunde`. Von Hand erfasste Werte
bleiben unangetastet, unsichere Werte werden als unbestätigt markiert, und ein
zweiter Lauf derselben Datei ändert nichts — die Ausgabe zählt das dann als
„unverändert".

Rückgabe-Code: 0 wenn die Datei gelesen und geschrieben wurde (auch wenn
einzelne Einträge fehlerhaft waren — die übrigen sind dann drin), sonst 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from moneten.db.session import SessionLocal  # noqa: E402
from moneten.routers.metrics import _kopf_pruefen, importiere_befunde  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Aufruf: verlaeufe_importieren.py <pfad zur verlaeufe.json>")
        return 1
    pfad = Path(argv[1])
    if not pfad.is_file():
        print(f"Datei nicht gefunden: {pfad}")
        return 1

    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        print(f"{pfad} ist kein UTF-8-Text.")
        return 1
    except json.JSONDecodeError as defekt:
        print(f"{pfad} ist kein gültiges JSON (Zeile {defekt.lineno}, Spalte {defekt.colno}).")
        return 1

    if (meldung := _kopf_pruefen(daten)) is not None:
        print(meldung)
        return 1

    with SessionLocal() as db:
        bericht = importiere_befunde(db, daten)

    print(
        f"  {bericht['neu']} neu, {bericht['aktualisiert']} aktualisiert, "
        f"{bericht['uebersprungen']} unverändert"
    )
    if bericht["unsicher"]:
        # Nur die Zahl: die Werte selbst stehen in der App und gehören nicht in
        # ein Deploy-Protokoll, das im Terminal stehen bleibt.
        print(f"  {len(bericht['unsicher'])} Werte sind unbestätigt "
              f"(in der App unter Verläufe prüfen)")
    for zeile in bericht["fehler"]:
        print(f"  ! {zeile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
