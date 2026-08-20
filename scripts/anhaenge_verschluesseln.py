"""Wandelt bereits abgelegte Beleg-Fotos ins verschlüsselte Format.

**Wozu.** Seither schreibt die App Beleg-Fotos verschlüsselt
(``services/anhang_tresor.py``). Was **vorher** abgelegt wurde, liegt weiter im
Klartext: gewöhnliche JPEGs in ``data/attachments/receipt_photos/``. Gelesen
werden sie weiterhin — der Tresor erkennt am Kopf, was vorliegt —, aber
geschützt sind sie nicht. Dieses Skript holt sie nach.

**Aufruf** (auf dem NAS, im laufenden Container — nur dort steht der Schlüssel)::

    docker exec moneten python /app/scripts/anhaenge_verschluesseln.py
    docker exec moneten python /app/scripts/anhaenge_verschluesseln.py --wirklich

Ohne ``--wirklich`` wird nur gezählt und angezeigt. Nichts wird angefasst.

**Warum in dieser Reihenfolge gewandelt wird.** Erst die neue Datei schreiben,
dann den Verweis in der Datenbank umbiegen, zuletzt das Original löschen. Bricht
der Lauf mittendrin ab, ist der schlimmste Fall eine verschlüsselte Kopie zu
viel — nie ein Verweis auf eine Datei, die es nicht mehr gibt. Ein erneuter Lauf
räumt das auf.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from sqlalchemy import select  # noqa: E402

from moneten.config import settings  # noqa: E402
from moneten.db.models import PendingReceipt  # noqa: E402
from moneten.db.session import SessionLocal  # noqa: E402
from moneten.services import anhang_tresor as tresor  # noqa: E402


def main() -> int:
    wirklich = "--wirklich" in sys.argv

    if not settings.db_key:
        print("Kein MONETEN_DB_KEY gesetzt — ohne Schlüssel gibt es nichts zu verschlüsseln.")
        return 1

    ordner = tresor.foto_ordner()
    if not ordner.is_dir():
        print(f"Kein Foto-Ordner unter {ordner} — nichts zu tun.")
        return 0

    offen: list[Path] = []
    schon = 0
    for datei in sorted(ordner.iterdir()):
        if not datei.is_file() or datei.suffix == ".teil":
            continue
        if tresor.ist_verschluesselt(datei.read_bytes()[: len(tresor.MAGIE)]):
            schon += 1
            continue
        offen.append(datei)

    print(f"{schon} Datei(en) bereits verschlüsselt, {len(offen)} im Klartext.")
    if not offen:
        return 0
    if not wirklich:
        for datei in offen[:20]:
            print(f"  würde wandeln: {datei.name}")
        if len(offen) > 20:
            print(f"  … und {len(offen) - 20} weitere")
        print("\nZum Ausführen dasselbe noch einmal mit  --wirklich")
        return 0

    gewandelt = 0
    with SessionLocal() as db:
        for datei in offen:
            roh = datei.read_bytes()
            neu = tresor.schreiben(datei, roh)

            # Der Verweis in der Datenbank. Vergleich über den aufgelösten Pfad,
            # weil gespeichert wurde, was der Container damals sah.
            alt_str = str(datei)
            for pend in db.scalars(
                select(PendingReceipt).where(PendingReceipt.image_path.is_not(None))
            ):
                if str(Path(pend.image_path).resolve()) == str(datei.resolve()) or (
                    pend.image_path == alt_str
                ):
                    pend.image_path = str(neu)
            db.commit()

            datei.unlink()
            gewandelt += 1

    print(f"{gewandelt} Datei(en) gewandelt. Der Ordner enthält jetzt nur noch Geheimtext.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
