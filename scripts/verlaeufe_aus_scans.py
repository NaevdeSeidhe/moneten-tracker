"""Einmal-Extraktion: Belege aus dem Scan-Ordner zu ``verlaeufe.json``.

**Läuft lokal, nicht auf dem NAS.** Die Scans liegen auf dem Arbeitsrechner, die
App auf dem NAS — ein Import „beim Start" könnte den Ordner also gar nicht
sehen. Dieses Skript liest die Belege hier, schreibt eine Datei mit den
gefundenen Werten, und die App nimmt diese Datei über die Verlaufsseite entgegen.
Der Umweg hat einen Nebennutzen: die Zahlen sind vor dem Import einsehbar.

Aufruf::

    python scripts/verlaeufe_aus_scans.py /pfad/zu/den/scans --ziel verlaeufe.json

Die Zuordnung Beleg → Parser läuft über Ordner- und Dateinamen, nicht über den
Inhalt. Das ist verlässlicher, solange die Scans konsequent benannt sind, und
es hält das Skript davon ab, in Belegen zu stöbern, die es nichts angehen:
Kontoauszüge und Quittungen werden gar nicht erst geöffnet.

Wer seine Ordner anders nennt, legt eine eigene Zuordnung an — siehe
``--zuordnung`` und :func:`lies_zuordnung`. Sie gehört zu den DATEN und nicht in
den Quelltext: Ordnernamen sagen mehr über einen Menschen aus als der Inhalt der
Belege.

Der Name entscheidet, WELCHER Parser einen Beleg bekommt — nicht, ob dieser
Beleg etwas hergibt. Liegen in einem Ordner zwei Dokumentarten (neben den
Rechnungen eines Anbieters auch dessen Nutzungsnachweise), unterscheidet sie
der Parser am Inhalt und gibt für die falsche Art nichts zurück. Ein Ordner
lässt sich umsortieren und eine Datei umbenennen; ein Rechnungskopf nicht.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from moneten.services import belege_parser as bp  # noqa: E402

# Die Spaltenextraktion steht im Paket und nicht hier: dieselbe Rechnung wird ein
# zweites Mal gelesen, wenn die App sie als Beleg an die Buchung hängt. Zwei
# Kopien der Koordinatenlogik würden auseinander driften.
from moneten.services.pdf_spalten import pdf_spalten  # noqa: E402

# Ordner, die dieses Skript niemals öffnet. Kontoauszüge und Quittungen sind
# für die Verlaufsreihen ohne Nutzen — die App liest sie über den CAMT-Import
# und den Belegscan, wo sie hingehören.
TABU = {"quittungen", "_backups", ".trashbox"}

# Wo die eigene Ordner-Zuordnung erwartet wird. **Am Skript verankert, nicht am
# Arbeitsverzeichnis.** Ein relativer Pfad war hier ein echter Fehler: der
# Starter ruft dieses Skript ohne gesetztes Arbeitsverzeichnis auf, die Datei
# wurde nicht gefunden, und damit fiel still auch die eigene Tabu-Liste weg —
# das Skript hätte in Ordnern gelesen, die es nie öffnen soll.
ZUORDNUNG_VORGABE = pathlib.Path(__file__).resolve().parents[1] / "data" / "zuordnung.toml"

# (Parser-Schlüssel, Ordner-Stichwort, Datei-Stichwörter)
# Ein Beleg wird genau einem Parser zugeordnet — der erste Treffer gewinnt.
ZUORDNUNG: list[tuple[str, str | None, tuple[str, ...]]] = [
    ("verbilligung", "praemienverbilligungen", ("praemienverbilligung", "verbilligung")),
    ("kk_praemie", "praemienabrechnung", ("praemienabrechnung",)),
    ("leistung", "leistungsabrechnungen", ("leistungsabrechnung",)),
    # VOR "police": diese Sachversicherungs-Police traegt „Versicherungspolice"
    # im Dateinamen und landete damit beim KRANKENkassen-Parser, der keine
    # KVG-Zeile fand und still nichts zurueckgab. Die spezifischere Regel
    # muss zuerst stehen — der erste Treffer gewinnt.
    ("hausrat", "versicherung", ("hausrat", "privathaftpflicht")),
    ("police", "policen", ("police", "versicherungspolice")),
    ("strom", "strom", ("stromrechnung",)),
    ("pk", "pensionskasse", ("vorsorgeausweis",)),
    ("miete", "wohnen", ("mietvertrag",)),
    ("steuern", "steuern", ("steuer", "bundessteuer", "gemeindesteuer")),
    # Lohnausweise liegen oft verstreut (mehrere Arbeitgeber-Ordner) — hier
    # entscheidet allein der Dateiname. Der Ordnerteil ist bewusst None, sonst
    # traefe ein leerer Suchbegriff auf JEDEN Ordner zu.
    ("lohnausweis", None, ("lohnausweis",)),
]

# Anbieter-Rechnungen: je Profil ein Eintrag, erkannt am Ordnernamen. Hier stand
# einmal ein fester Anbietername; jetzt kommt er aus den Profildateien (siehe
# ``moneten.services.anbieter_profil``), und ein weiterer Anbieter ist eine
# Datei statt einer Code-Änderung.
#
# Der Ordner reicht, der Dateiname wird bewusst NICHT befragt: neben den
# Rechnungen liegen Nutzungsnachweise ohne Rechnungsbetrag, deren Zeilen fast
# durchgehend 0.00 ausweisen. Sie auseinanderzuhalten ist Sache des Parsers, der
# den Inhalt sieht — ein Dateiname lässt sich umbenennen, ein Rechnungskopf nicht.
#
# Angehängt und nicht eingefügt: in der Liste oben entscheidet die Reihenfolge
# (die spezifischere Regel zuerst), und die Anbieter-Stichwörter sind
# unverwechselbar genug, um am Ende zu stehen.
ZUORDNUNG += [
    (profil.slug, profil.stichwort, ())
    for profil in bp.PROFILE.values()
]


def entschaerft(text: str) -> str:
    """Kleinbuchstaben ohne Umlaute — für Namensvergleiche.

    Die Scans mischen ``Prämienabrechnung`` und ``Praemienabrechnung``; ohne
    Normalisierung fände die Zuordnung mal die eine, mal die andere Hälfte.
    """
    zerlegt = unicodedata.normalize("NFKD", text.lower())
    ohne = "".join(z for z in zerlegt if not unicodedata.combining(z))
    return ohne.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def lies_zuordnung(pfad: pathlib.Path) -> None:
    """Eigene Ordner-Zuordnung dazunehmen — sie gewinnt vor den Vorgaben.

    Warum als Datei und nicht im Skript: die Vorgaben oben heissen nach
    SACHEN (``policen``, ``strom``, ``pensionskasse``). Wer seine Scans anders
    sortiert, hat Ordner, die nach seinem Vermieter, seinem Kanton oder seiner
    Bank heissen — Namen, die niemanden etwas angehen und die deshalb bei den
    Daten liegen, nicht im Quelltext.

    Format (TOML)::

        tabu = ["hausbank"]              # Ordner, die nie geoeffnet werden

        [[zuordnung]]
        parser  = "miete"
        ordner  = "2019_wohnung_am_park"
        dateien = ["mietvertrag"]

    Eigene Eintraege stehen VORNE: in der Zuordnung gewinnt der erste Treffer,
    und die eigene Regel ist die speziellere.
    """
    if not pfad.is_file():
        return
    import tomllib

    daten = tomllib.loads(pfad.read_text(encoding="utf-8"))
    TABU.update(entschaerft(t) for t in daten.get("tabu", []))
    eigene: list[tuple[str, str | None, tuple[str, ...]]] = []
    for eintrag in daten.get("zuordnung", []):
        parser = eintrag.get("parser")
        if not parser:
            raise ValueError(f"{pfad.name}: ein Eintrag ohne 'parser'")
        eigene.append((parser, eintrag.get("ordner"), tuple(eintrag.get("dateien", ()))))
    ZUORDNUNG[:0] = eigene


def parser_fuer(relativ: pathlib.Path) -> str | None:
    """Welcher Parser für diesen Beleg zuständig ist — oder keiner.

    Der Pfad kommt **relativ zur Scan-Wurzel** herein, und gesucht wird über die
    ganze Ordnerkette, nicht nur im unmittelbaren Elternordner. Der Grund ist
    ein gemessener Ausfall: die Rechnungen eines Anbieters wanderten in
    Unterordner (``<Anbieter>/Rechnungen``, ``<Anbieter>/Nutzungsnachweise``),
    damit sich der Ordner als Ganzes spiegeln lässt — und ab da hiess der
    Elternordner „Rechnungen", das Stichwort des Anbieters traf nicht mehr, und
    kein einziger Beleg wurde noch zugeordnet. Wer seine Scans sortiert, soll
    nicht die Extraktion verlieren.

    Relativ, nicht absolut: über dem Scan-Ordner liegen Verzeichnisnamen, die
    mit den Belegen nichts zu tun haben (der Benutzerordner, ein Laufwerksname).
    Sie dürfen die Zuordnung nicht beeinflussen.
    """
    ordner = entschaerft("/".join(relativ.parent.parts))
    name = entschaerft(relativ.name)
    for schluessel, ordner_wort, datei_woerter in ZUORDNUNG:
        if ordner_wort and entschaerft(ordner_wort) in ordner:
            return schluessel
        if any(entschaerft(w) in name for w in datei_woerter):
            return schluessel
    return None


def pdf_text(pfad: pathlib.Path) -> str:
    """Textebene eines PDFs. Leer, wenn es ein reiner Bildscan ist."""
    import fitz  # lokal: nur dieses Skript braucht PyMuPDF

    with fitz.open(pfad) as doc:
        return "".join(seite.get_text() for seite in doc)


# So viele Zeichen muss ein Beleg mindestens hergeben, damit sich das Parsen
# lohnt. Darunter ist es ein Deckblatt oder ein Scan ohne Textebene.
MIN_TEXT = 200


def ocr_text(pfad: pathlib.Path) -> str:
    """Text eines gescannten PDF — ueber dieselbe Erkennung wie der Belegscan.

    Kein zweiter OCR-Weg: was die App beim Foto einer Quittung liest, liest hier
    dieselbe Funktion. Zwei Erkennungen wuerden auseinander driften, und der
    Unterschied faellt erst an einem falsch gelesenen Betrag auf.
    """
    try:
        from moneten.services.receipt_ocr import extract_text
        return extract_text(str(pfad)).text or ""
    except Exception:  # noqa: BLE001 — fehlende OCR-Engine darf den Lauf nicht kippen
        return ""


def sammle(wurzel: pathlib.Path) -> tuple[list[dict[str, object]], Counter[str]]:
    """Alle Belege lesen und die Befunde einsammeln."""
    befunde: list[dict[str, object]] = []
    zaehler: Counter[str] = Counter()

    for pfad in sorted(wurzel.rglob("*.pdf")):
        relativ = pfad.relative_to(wurzel)
        if any(entschaerft(teil) in TABU for teil in relativ.parts):
            zaehler["tabu"] += 1
            continue
        schluessel = parser_fuer(relativ)
        if schluessel is None:
            zaehler["nicht zugeordnet"] += 1
            continue
        lesen = pdf_spalten if schluessel in bp.SPALTENTEXT else pdf_text
        try:
            text = lesen(pfad)
        except Exception as fehler:  # noqa: BLE001 — ein kaputtes PDF darf den Lauf nicht kippen
            zaehler["nicht lesbar"] += 1
            print(f"  ! {pfad.name}: {fehler}", file=sys.stderr)
            continue
        if len(text.strip()) < MIN_TEXT and schluessel not in bp.SPALTENTEXT:
            # OCR-Rueckfall. Ohne ihn verschwand jeder GESCANNTE Beleg
            # stillschweigend in der Zeile „ohne Textebene": die
            # Hausrat-/Privathaftpflicht-Police liegt seit Juli 2024 im Ordner,
            # ist ein reines Bild-PDF, und die Reihe blieb darum leer, ohne dass
            # irgendwo stand warum.
            #
            # NICHT fuer Spaltentext-Parser: die brauchen Koordinaten aus der
            # Textebene, und OCR liefert sie in anderer Form. Ein Beleg ohne
            # Textebene ist dort wirklich keiner.
            text = ocr_text(pfad)
            if len(text.strip()) < MIN_TEXT:
                zaehler["ohne Textebene"] += 1
                continue
            zaehler["per OCR gelesen"] += 1

        try:
            gefunden = bp.PARSER[schluessel](text)
        except bp.PruefsummeFehler as fehler:
            # Die Selbstprüfung ist der Sinn des Parsers: geht eine Rechnung
            # nicht auf, ist sie falsch gelesen. Der Lauf geht weiter, aber
            # dieser Beleg liefert KEINEN Wert — ein stiller falscher Betrag
            # wäre schlimmer als der fehlende Monat.
            zaehler[f"{schluessel}: Selbstprüfung fehlgeschlagen"] += 1
            print(f"  ! {pfad.name}: {fehler}", file=sys.stderr)
            continue
        if not gefunden:
            zaehler[f"{schluessel}: nichts gefunden"] += 1
            continue
        zaehler[f"{schluessel}: gelesen"] += 1
        for b in gefunden:
            # Ein falsch gegriffener Wert ist schlimmer als ein fehlender: er
            # sähe im Verlauf aus wie eine echte Beobachtung.
            if not bp.plausibel(b.slug, b.value, null_bestaetigt=b.null_bestaetigt):
                zaehler[f"{b.slug}: unplausibel verworfen"] += 1
                print(f"  ? {pfad.name}: {b.slug} = {b.value} — "
                      f"{bp.unplausibel_warum(b.slug, b.value)}", file=sys.stderr)
                continue
            befunde.append({
                "slug": b.slug,
                "start": b.period_start.isoformat(),
                "ende": b.period_end.isoformat(),
                "wert": str(b.value),
                "extras": b.extras,
                "unsicher": b.unsicher,
                "hinweis": b.hinweis,
                "additiv": b.additiv,
                "null_bestaetigt": b.null_bestaetigt,
                "quelle": pfad.name,
            })
    return befunde, zaehler


def _addiert(alt: str, neu: str) -> str:
    """Zwei Nebenwerte zusammenzählen — oder den neueren nehmen, wenn es keine Zahlen sind.

    Nicht jeder Nebenwert ist ein Betrag: ``rechnungsart`` hält „Jahresrechnung",
    ``modell`` den Namen des Versicherungsmodells. Ohne diese Weiche riss die
    Summenbildung den ganzen Lauf mit einer ``InvalidOperation`` ab, sobald ein
    Parser mit Text-Nebenwerten je additiv würde.
    """
    try:
        return str(Decimal(alt) + Decimal(neu))
    except InvalidOperation:
        return neu


def zusammenfassen(befunde: list[dict[str, object]]) -> list[dict[str, object]]:
    """Je Reihe und Periode einen Wert erzeugen — je nach Reihe durch Auswahl oder Summe.

    **Zwei verschiedene Fälle, die nicht verwechselt werden dürfen.**

    Nicht-additive Reihen beschreiben dieselbe Grösse mehrfach: eine Nachbelastung
    „Jan–Mai" deckt Monate ab, für die schon Einzelabrechnungen vorliegen; für 2024
    existieren zwei Policen, weil das Versicherungsmodell wechselte. Hier gewinnt
    der **spezifischere** Beleg — der mit der kürzeren Periode. Bei gleicher Länge
    der zuletzt gelesene, weil die Dateinamen chronologisch sortiert sind und der
    jüngere Beleg den älteren korrigiert.

    Additive Reihen beschreiben je einen **Teil** der Periode: eine
    Leistungsabrechnung ist eine Behandlung, die Jahressumme entsteht erst aus
    allen zusammen. Würde hier ausgewählt statt summiert, stünde im Verlauf der
    Betrag einer beliebigen Arztrechnung.

    **Monatswerte gehören in den ersten Fall, auch die aufgeschlüsselten.** Eine
    Anbieter-Rechnung deckt einen Monat vollständig ab; träfen zwei Belege auf
    denselben Monat, wäre einer davon eine Korrektur und nicht die Hälfte der
    Summe. Additiv geführt ergäbe der Monat den doppelten Betrag, und die
    ``pos:``-Positionen zählten jede Zeile zweimal — genau die Gleichung, die
    der Parser eigens gegen den Beleg prüft, wäre danach still verletzt.
    """
    def spanne(b: dict[str, object]) -> int:
        return (
            int(str(b["ende"]).replace("-", "")) - int(str(b["start"]).replace("-", ""))
        )

    beste: dict[tuple[str, str], dict[str, object]] = {}
    for b in befunde:
        schluessel = (str(b["slug"]), str(b["start"]))
        alt = beste.get(schluessel)
        if alt is None:
            beste[schluessel] = dict(b)
            continue
        if b.get("additiv"):
            alt["wert"] = str(Decimal(str(alt["wert"])) + Decimal(str(b["wert"])))
            alt_extras = dict(alt.get("extras") or {})
            for k, v in (b.get("extras") or {}).items():  # type: ignore[union-attr]
                # Beträge aufsummieren, Grenzwerte wie die Jahresfranchise ersetzen:
                # sie gilt fürs ganze Jahr und darf sich nicht vervielfachen.
                if k == "jahresfranchise" or k not in alt_extras:
                    alt_extras[k] = str(v)
                else:
                    alt_extras[k] = _addiert(alt_extras[k], str(v))
            alt["extras"] = alt_extras
            alt["quelle"] = f"{alt.get('quelle')} u.a."
        elif spanne(b) <= spanne(alt):
            beste[schluessel] = dict(b)
    return sorted(beste.values(), key=lambda b: (str(b["slug"]), str(b["start"])))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ordner", type=pathlib.Path, help="Wurzel des Scan-Ordners")
    p.add_argument("--ziel", type=pathlib.Path, default=pathlib.Path("verlaeufe.json"))
    # Von Hand gelesene Werte. Sie liegen BEWUSST ausserhalb des Repos: die
    # Datei enthaelt echte Betraege, und das Projektverzeichnis wandert aufs NAS,
    # ins Backup und spaeter womoeglich weiter. Was das OCR zerstoert hat, wird
    # hier nachgetragen statt im Code.
    p.add_argument("--ergaenzung", type=pathlib.Path, default=None,
                   help="JSON mit von Hand gelesenen Befunden, wird dazugemischt")
    # Die eigene Ordner-Zuordnung. Vorgabe ist die Datendatei der App — dort
    # liegt sie neben den Anbieterprofilen und wandert mit denselben Regeln
    # nirgendwo mit.
    p.add_argument("--zuordnung", type=pathlib.Path, default=ZUORDNUNG_VORGABE,
                   help=f"TOML mit eigener Ordner-Zuordnung (Vorgabe: {ZUORDNUNG_VORGABE})")
    args = p.parse_args()

    # Sichtbar machen, WELCHE Zuordnung gilt. Dieses Skript wird von einem
    # Starter aufgerufen; ein stilles „nicht gefunden" hiesse hier: die eigenen
    # Ordner werden nicht mehr erkannt UND die Tabu-Liste ist kürzer als
    # gedacht — es würde also in Ordnern gelesen, die tabu sein sollten. Beides
    # sieht man am Ergebnis erst Wochen später, wenn überhaupt.
    if args.zuordnung.is_file():
        print(f"Zuordnung: {args.zuordnung}")
    else:
        print(f"Zuordnung: keine eigene gefunden ({args.zuordnung}) — es gelten nur die Vorgaben")
    lies_zuordnung(args.zuordnung)

    if not args.ordner.is_dir():
        print(f"Ordner nicht gefunden: {args.ordner}", file=sys.stderr)
        return 1

    roh, zaehler = sammle(args.ordner)

    if args.ergaenzung:
        if not args.ergaenzung.is_file():
            print(f"Ergaenzung nicht gefunden: {args.ergaenzung}", file=sys.stderr)
            return 1
        zusatz = json.loads(args.ergaenzung.read_text(encoding="utf-8"))["befunde"]
        for b in zusatz:
            b.setdefault("extras", {})
            b.setdefault("unsicher", False)
            b.setdefault("additiv", False)
            b.setdefault("hinweis", "")
            b.setdefault("null_bestaetigt", False)
        # Auch von Hand gelesene Werte laufen durch die Plausibilitaetspruefung.
        # Nicht aus Misstrauen gegen den Leser, sondern gegen den Tippfehler: eine
        # verrutschte Kommastelle faellt hier auf, und eine gewollte Null muss
        # sich mit "null_bestaetigt" dazu bekennen, statt stillschweigend
        # durchzurutschen — sonst waere die Regel im Parser nur die halbe Miete.
        for b in zusatz:
            wert = Decimal(str(b["wert"]))
            if not bp.plausibel(str(b["slug"]), wert, null_bestaetigt=bool(b["null_bestaetigt"])):
                zaehler[f"{b['slug']}: unplausibel verworfen"] += 1
                print(f"  ? Ergaenzung: {b['slug']} {b['start']} = {wert} — "
                      f"{bp.unplausibel_warum(str(b['slug']), wert)}", file=sys.stderr)
                continue
            # ANS ENDE: bei gleicher Periode und gleicher Spannenlaenge gewinnt der
            # zuletzt gelesene Eintrag. Von Hand geprueft schlaegt maschinell geraten.
            roh.append(b)
            zaehler["von Hand ergaenzt"] += 1

    befunde = zusammenfassen(roh)

    args.ziel.write_text(
        json.dumps({"version": 1, "befunde": befunde}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{len(befunde)} Werte in {args.ziel}")
    if verworfen := len(roh) - len(befunde):
        print(f"  {verworfen} doppelte Perioden zusammengefasst")
    for was, wie_viele in sorted(zaehler.items()):
        print(f"  {wie_viele:4}  {was}")
    unsicher = sum(1 for b in befunde if b["unsicher"])
    if unsicher:
        print(f"\n  {unsicher} Werte sind als unsicher markiert (OCR-Quelle)")
        print("  Die App legt sie beim Import einzeln zur Bestätigung vor.")

    summen: Counter[str] = Counter(str(b["slug"]) for b in befunde)
    print()
    for slug, anzahl in sorted(summen.items()):
        werte = [Decimal(str(b["wert"])) for b in befunde if b["slug"] == slug]
        print(f"  {slug:20} {anzahl:3} Werte   {min(werte)} bis {max(werte)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
