"""Schritt-für-Schritt-Abgleich-Jobs mit Fortschritt (für den runden Ring).

Bewusst **ohne** Hintergrund-Thread: der Client (HTMX) pollt und treibt damit
die Verarbeitung voran — jeder Poll erledigt **eine** Quittung (OCR + Abgleich)
und meldet den Fortschritt zurück. Das ist mit SQLite/SQLCipher gefahrlos
(jede Anfrage hat ihre eigene DB-Session, kein Cross-Thread-Zugriff) und der
Nutzer sieht den Stand live. Schliesst er die Seite, pausiert die Arbeit und
läuft beim erneuten Öffnen weiter.

Job-Status liegt im Speicher (Single-User, eine Instanz) — kein Persistenz-Bedarf.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

# job_id -> {names, idx, matched, total, current, done}
_JOBS: dict[str, dict[str, Any]] = {}


def _prune_done() -> None:
    """Abgeschlossene Jobs aufräumen (verhindert unbegrenztes Wachsen)."""
    for jid in [k for k, v in _JOBS.items() if v.get("done")]:
        _JOBS.pop(jid, None)


def start_match_job(db: Session) -> str:
    """Legt einen Abgleich-Job über alle aktuell unzugeordneten Quittungen an."""
    from moneten.services.receipt_match import auto_archive_old, unassigned_receipts

    _prune_done()
    auto_archive_old(db)  # alte Belege (vor Banktabellen-Start) vorher ablegen
    names = [r.name for r in unassigned_receipts(db)]
    jid = uuid.uuid4().hex[:12]
    _JOBS[jid] = {
        "names": names, "idx": 0, "matched": 0,
        "total": len(names), "current": "", "done": len(names) == 0,
        # Zwischenspeicher für gelesenen Text und Rechnungs-Befund je Datei.
        # Ohne ihn zahlte der zweite Durchgang (siehe :func:`step_match_job`)
        # die OCR jeder wieder eingereihten Datei ein zweites Mal.
        "cache": {}, "runden": 1,
    }
    return jid


def get_job(jid: str) -> dict[str, Any] | None:
    return _JOBS.get(jid)


def step_match_job(db: Session, jid: str) -> dict[str, Any] | None:
    """Verarbeitet die **nächste** Quittung des Jobs (OCR + Auto-Zuordnung).

    Gibt den aktualisierten Job-Status zurück (oder None, wenn unbekannt).
    """
    from moneten.services.attachments import list_receipts
    from moneten.services.receipt_match import auto_match_one

    job = _JOBS.get(jid)
    if job is None or job["done"]:
        return job

    by_name = {r.name: r for r in list_receipts()}
    name = job["names"][job["idx"]]
    job["current"] = name
    receipt = by_name.get(name)
    if receipt is not None and auto_match_one(db, receipt, cache=job["cache"]):
        job["matched"] += 1

    job["idx"] += 1
    if job["idx"] >= job["total"] and not _neue_runde(db, job):
        job["done"] = True
        job["current"] = ""
    return job


# Hoechstzahl der Durchgaenge. Zwoelf Monatsrechnungen brauchen im schlimmsten
# Fall zwoelf; darueber hinaus ist etwas anderes im Argen.
_MAX_RUNDEN = 15


def _neue_runde(db: Session, job: dict[str, Any]) -> bool:
    """Haengt die noch offenen Belege erneut an — True, wenn eine Runde folgt.

    **Warum ueberhaupt ein zweiter Durchgang.** Die Anbieter-Rechnungen bilden
    eine Kette: die des Februars darf die Januar-Buchung nur uebergehen, wenn
    dort schon die Januar-Rechnung haengt (``rechnungsbeleg.passende_buchung``).
    Solange die Dateien nach Datum benannt sind, geht das in einem Durchgang auf
    — aber genau darauf zu bauen hiesse, die Zuordnung von der Schreibweise der
    Dateinamen abhaengig zu machen.

    **Angehaengt statt neu gesetzt.** ``total`` waechst, ``idx`` laeuft weiter;
    der Fortschrittsbalken springt damit nie zurueck. Ihn auf die Restmenge
    zurueckzusetzen sahe aus, als finge der Abgleich von vorne an.

    Eine Runde folgt nur, wenn die vorige etwas zugeordnet hat — sonst braechte
    die naechste dasselbe Ergebnis.
    """
    from moneten.services.receipt_match import unassigned_receipts

    if job["matched"] <= job.get("matched_bei_rundenstart", 0):
        return False
    if job["runden"] >= _MAX_RUNDEN:
        return False
    bekannt = set(job["names"])
    # NUR gelesene Rechnungen bekommen eine zweite Chance. Alle offenen Belege
    # erneut einzureihen waere quadratisch: 50 Belege, von denen je Runde einer
    # trifft, ergaeben 50 Runden mit je 50 Schritten — und jeder Schritt ist ein
    # Aufruf aus dem Browser. Die Kettenabhaengigkeit haben ausserdem nur die
    # Rechnungen; ein Kassenbon, der nicht getroffen hat, trifft auch nach einer
    # fremden Zuordnung nicht. Woran man sie erkennt, steht schon im
    # Zwischenspeicher dieser Runde.
    kette = [
        r.name for r in unassigned_receipts(db)
        if r.name in bekannt and job["cache"].get(("rechnung", r.path)) is not None
    ]
    if not kette:
        return False
    job["names"] = job["names"] + kette
    job["total"] += len(kette)
    job["runden"] += 1
    job["matched_bei_rundenstart"] = job["matched"]
    return True


def job_percent(job: dict[str, Any]) -> int:
    total = job.get("total") or 0
    return 100 if total == 0 else round(job["idx"] / total * 100)


# ---------------------------------------------------------------------------
# Beleg-Scan als Auftrag
# ---------------------------------------------------------------------------
#
# Der Foto-Scan war ein synchroner POST: der Browser lud das Bild hoch und
# wartete, bis die Erkennung fertig war. Auf der NAS-CPU sind das Minuten — und
# wer waehrenddessen kurz in eine andere App wechselt, kommt zurueck und findet
# nichts mehr vor. Das Handy haelt eine Seite im Hintergrund nicht am Leben, die
# Anfrage stirbt mit ihr, und die Erkennung war NUR in dieser Antwort.
#
# Jetzt laeuft die Erkennung in einem Thread und legt ihr Ergebnis unter einer
# Nummer ab. Der Browser fragt diese Nummer ab, so oft er will und wann immer er
# zurueckkommt. Anders als beim Abgleich-Job (dort treibt der Poll die Arbeit an)
# muss hier wirklich ein Thread laufen: die Erkennung ist EIN langer Schritt, der
# sich nicht in Haeppchen zerlegen laesst.
#
# Es wird KEINE Datenbank angefasst: der Thread liest nur Bytes und gibt Text
# zurueck. Die Auswertung (`analyze`) passiert spaeter in der Anfrage, die das
# Ergebnis abholt — die hat ihre eigene Session.

# scan_id -> {"zustand": laeuft|fertig|fehler, "ocr": OcrResult|None,
#             "bild": str|None, "fehler": str}
_SCANS: dict[str, dict[str, Any]] = {}

# Wie viele abgeschlossene Scans aufgehoben werden. Genug, dass ein Nutzer nach
# dem Zurueckkommen noch seinen findet; klein genug, dass der Speicher nicht
# waechst. Ein OcrResult ist Text, kein Bild — das Foto wird nicht behalten.
_SCAN_HISTORIE = 8

#: Wie viele Erkennungen gleichzeitig laufen duerfen.
#:
#: Begrenzt war bisher nur die HISTORIE fertiger Auftraege, nicht die Zahl der
#: laufenden: jeder Upload bekam einen eigenen Thread mit eigenen Bilddaten.
#: Zwei gleichzeitig abgeschickte Grossformat-PDFs reissen zusammen das
#: Speicherlimit, auch wenn keines allein es tut — und auf dem Handy genuegt
#: dafuer zweimaliges Antippen des Absendens.
#:
#: Zwei statt eins: der DS218+ hat zwei Kerne, und die Oberflaeche wartet
#: ohnehin per Abfrage auf das Ergebnis.
_GLEICHZEITIG = threading.Semaphore(2)

#: Wie viele Auftraege insgesamt offen sein duerfen — laufende UND wartende.
#:
#: Die Schranke oben begrenzt, wie viele Erkennungen gleichzeitig RECHNEN. Sie
#: begrenzt nicht, wie viele Auftraege davor Schlange stehen: jeder wartende
#: haelt seine Bilddaten die ganze Zeit im Speicher fest — bis zu 15 MB, dazu
#: ein eigener Thread. Zehnmal auf „Absenden" getippt sind 150 MB in einem
#: Container mit 1 GB, und das Bild wird erst beim Aufraeumen des Auftrags
#: wieder freigegeben.
#:
#: Vier: zwei rechnen, zwei warten. Wer den fuenften schickt, bekommt sofort
#: eine klare Antwort statt eines Auftrags, der ohnehin nur Speicher belegt.
MAX_OFFENE_SCANS = 4


class ZuVieleScans(RuntimeError):
    """Es stehen schon genug Erkennungen an."""


#: Nach dieser Zeit gilt ein Auftrag als verloren.
#:
#: Die Plaetze sind begrenzt (siehe MAX_OFFENE_SCANS). Ohne diese Frist genuegt
#: EIN haengender Auftrag, um einen Platz bis zum Neustart des Containers zu
#: belegen — vier davon, und die App nimmt keinen Beleg mehr an, ohne dass
#: irgendwo etwas dazu steht. Haengen kann es an Stellen, die wir nicht in der
#: Hand haben: ein Unterprozess, der nicht zurueckkommt, ein Modell, das sich
#: verklemmt.
#:
#: Zehn Minuten sind reichlich: die Erkennung selbst hat eine Frist von drei
#: Minuten je Aufruf, und mehr als zwei laufen nie gleichzeitig.
_HOECHSTDAUER_SEKUNDEN = 600


def _verlorene_aufgeben() -> None:
    """Haengende Auftraege als gescheitert markieren — sie geben den Platz frei.

    Der Thread laeuft womoeglich weiter; kommt er doch noch zurueck, schreibt er
    sein Ergebnis und der Nutzer kann es abholen. Was er nicht mehr tut, ist
    einen Platz zu blockieren.
    """
    jetzt = time.monotonic()
    for eintrag in _SCANS.values():
        if eintrag["zustand"] != "laeuft":
            continue
        begonnen = eintrag.get("begonnen")
        if begonnen is not None and jetzt - begonnen > _HOECHSTDAUER_SEKUNDEN:
            eintrag.update(
                zustand="fehler",
                fehler="Zeitüberschreitung — die Erkennung hat nicht geantwortet.",
            )


def offene_scans() -> int:
    """Wie viele Erkennungen gerade laufen oder warten."""
    _verlorene_aufgeben()
    return sum(1 for v in _SCANS.values() if v["zustand"] == "laeuft")


def _prune_scans() -> None:
    fertig = [k for k, v in _SCANS.items() if v["zustand"] != "laeuft"]
    for jid in fertig[:-_SCAN_HISTORIE] if len(fertig) > _SCAN_HISTORIE else []:
        _SCANS.pop(jid, None)


def start_scan_job(data: bytes, suffix: str, *, bild_speichern) -> str:
    """Startet die Erkennung eines Fotos im Hintergrund und gibt ihre Nummer.

    ``bild_speichern`` ist eine Funktion ``bytes -> str|None``; sie wird im
    Thread aufgerufen, damit auch das Verkleinern und Ablegen des Fotos nicht in
    der Anfrage haengt. Wer das Foto nicht behalten will, uebergibt ``None``.
    """
    import threading

    from moneten.services.receipt_ocr import extract_text_from_bytes

    _prune_scans()
    # Zweite Stelle mit derselben Pruefung wie in der Route. Absicht: die Route
    # prueft, BEVOR sie 15 MB einliest (dort spart es den Speicher), diese hier
    # gilt fuer jeden kuenftigen Aufrufer, der das vergisst.
    if offene_scans() >= MAX_OFFENE_SCANS:
        raise ZuVieleScans(f"Es laufen bereits {offene_scans()} Erkennungen.")
    jid = uuid.uuid4().hex[:12]
    _SCANS[jid] = {"zustand": "laeuft", "ocr": None, "bild": None, "fehler": "",
                   "begonnen": time.monotonic()}

    def arbeiten() -> None:
        try:
            with _GLEICHZEITIG:
                ergebnis = extract_text_from_bytes(data, suffix)
            bild = bild_speichern(data) if bild_speichern else None
            _SCANS[jid].update(zustand="fertig", ocr=ergebnis, bild=bild)
        except Exception as fehler:  # noqa: BLE001 — der Thread darf nie still sterben
            _SCANS[jid].update(zustand="fehler", fehler=str(fehler) or fehler.__class__.__name__)

    # **Erst wenn der Thread wirklich laeuft, ist der Platz belegt.** Der
    # Eintrag entsteht vorher (der Thread braucht ihn), aber wenn das Starten
    # scheitert — kein Thread mehr frei, Speicher am Ende —, bliebe er auf
    # "laeuft" stehen und belegte einen der vier Plaetze fuer immer. Nach vier
    # solchen Fehlstarts nimmt die App keinen Beleg mehr an, bis jemand den
    # Container neu startet, und niemand wuesste warum.
    try:
        threading.Thread(target=arbeiten, name=f"beleg-scan-{jid}", daemon=True).start()
    except RuntimeError as fehler:
        _SCANS[jid].update(zustand="fehler", fehler=f"Kein Thread frei: {fehler}")
        raise
    return jid


def scan_job(jid: str) -> dict[str, Any] | None:
    """Stand eines Scan-Auftrags — ``None``, wenn die Nummer unbekannt ist.

    Unbekannt heisst in der Praxis: der Server wurde neu gestartet oder der
    Auftrag ist aus der Historie gefallen. Die Oberflaeche muss beides gleich
    behandeln — sie kann den Beleg nur neu aufnehmen lassen.
    """
    return _SCANS.get(jid)
