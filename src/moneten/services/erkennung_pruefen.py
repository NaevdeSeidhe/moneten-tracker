"""Den Beleg-Bestand als Prüfstand: was würde die heutige Erkennung daraus machen?

**Warum es das gibt.** Jeder gescannte Beleg hat seinen Rohtext behalten
(``Attachment.ocr_text``). Damit liegt in der App eine Sammlung echter
Belegtexte — quer über Läden, Papierqualitäten und Kassensysteme. Bisher wurde
sie nie befragt: eine Verbesserung am Parser liess sich nur an dem einen Beleg
prüfen, der gerade gemeldet worden war, und ob sie anderswo etwas kaputt
machte, zeigte sich beim nächsten Scan.

Hier läuft die HEUTIGE Erkennung noch einmal über die GESPEICHERTEN Texte. Das
beantwortet zwei Fragen auf einmal:

* **Wo steht jetzt Falsches?** Belege, deren gespeicherte Positionen den Total
  nicht ergeben, tragen eine falsche Aufteilung — die wandert in Budget und
  Preisverlauf.
* **Was würde eine Neuauswertung ändern?** Wo die heutige Erkennung aufgeht und
  die alte nicht, ist eine Korrektur zu holen. Wo es umgekehrt ist, hat eine
  Änderung am Parser etwas verschlechtert — und das soll auffallen, bevor es
  jemandem im Alltag begegnet.

**Es wird nichts verändert.** Diese Datei liest und rechnet; das Umschreiben ist
eine eigene, ausdrückliche Handlung (:func:`neu_auswerten`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Attachment
from moneten.services.receipt_ocr import extract_amount
from moneten.services.receipt_split import parse_receipt_items_menge

# Dieselbe Toleranz wie die Gegenprobe im Editor — zwei Zahlen für dieselbe
# Frage wären zwei Antworten.
TOLERANZ = Decimal("0.02")


@dataclass(frozen=True)
class Befund:
    """Ein Beleg und was die heutige Erkennung anders sähe."""

    attachment_id: int
    haendler: str
    # Was gespeichert ist.
    alt_total: Decimal | None
    alt_positionen: int
    alt_geht_auf: bool
    # Was die heutige Erkennung aus demselben Rohtext machen würde.
    neu_total: Decimal | None
    neu_positionen: int
    neu_geht_auf: bool
    ocr_text: str

    @property
    def gewonnen(self) -> bool:
        """Heute geht es auf, gespeichert ist es falsch — hier ist etwas zu holen."""
        return self.neu_geht_auf and not self.alt_geht_auf

    @property
    def verloren(self) -> bool:
        """Gespeichert geht es auf, heute nicht mehr — eine Verschlechterung.

        Das ist der Befund, der wehtun soll: eine Änderung am Parser hat einen
        Beleg kaputtgemacht, der vorher stimmte.
        """
        return self.alt_geht_auf and not self.neu_geht_auf

    @property
    def unveraendert(self) -> bool:
        return (self.alt_total == self.neu_total
                and self.alt_positionen == self.neu_positionen)


def _dezimal(roh) -> Decimal | None:
    try:
        wert = Decimal(str(roh))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return wert if wert.is_finite() else None


def _geht_auf(positionen: list[Decimal], total: Decimal | None) -> bool:
    """Ergeben die Positionen den Total — und beweist das etwas?

    ``False`` bei fehlendem Total oder ohne Positionen: beides heisst, dass sich
    die Aufteilung nicht prüfen lässt, und ungeprüft ist nicht dasselbe wie
    richtig.

    **Und ``False`` bei GENAU EINER Position, die dem Total entspricht.** Dann
    ist „Summe = Total" keine Probe, sondern eine Identität: sie geht auch dann
    auf, wenn die einzige gefundene „Position" in Wahrheit die Totalzeile war.
    Genau so sah es an einem echten Bestand aus — 13 Belege meldeten „1 Pos. ✓" bei
    einem Betrag, der dem Total entsprach, und ein Klick auf „neu auswerten"
    hätte diese Scheinposition in den Beleg geschrieben.

    Dieselbe Regel gilt beim Lernen (``receipt_digital._nur_scheinbar_geprueft``)
    — sie hier zu vergessen hiess, mit zweierlei Mass zu messen.
    """
    if total is None or not positionen:
        return False
    if len(positionen) == 1 and abs(positionen[0] - total) <= TOLERANZ:
        return False
    return abs(sum(positionen, Decimal("0")) - total) <= TOLERANZ


def pruefe(db: Session) -> list[Befund]:
    """Jeden gespeicherten Beleg mit Rohtext noch einmal auswerten.

    Sortiert: erst die Verschlechterungen (die müssen jemanden erreichen), dann
    das zu Holende, dann der Rest.
    """
    befunde: list[Befund] = []
    for att in db.scalars(
        select(Attachment).where(Attachment.ocr_text.isnot(None),
                                 Attachment.parsed_items_json.isnot(None))
    ):
        text = att.ocr_text or ""
        if not text.strip():
            continue
        try:
            gespeichert = json.loads(att.parsed_items_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(gespeichert, dict):
            continue

        alt_preise = [p for p in (_dezimal(e.get("price"))
                                  for e in gespeichert.get("items") or []) if p is not None]
        alt_total = _dezimal(gespeichert.get("amount"))

        neu_items = parse_receipt_items_menge(text)
        neu_preise = [p for _n, p, _m in neu_items]
        neu_total = extract_amount(text)

        befunde.append(Befund(
            attachment_id=att.id,
            haendler=str(gespeichert.get("merchant") or att.original_name or "—"),
            alt_total=alt_total,
            alt_positionen=len(alt_preise),
            alt_geht_auf=_geht_auf(alt_preise, alt_total),
            neu_total=neu_total,
            neu_positionen=len(neu_preise),
            neu_geht_auf=_geht_auf(neu_preise, neu_total),
            ocr_text=text,
        ))
    return sorted(befunde, key=lambda b: (not b.verloren, not b.gewonnen, b.haendler.lower()))


@dataclass(frozen=True)
class Bilanz:
    """Der Stand in Zahlen — die eine Zeile, die man wirklich liest."""

    gesamt: int
    alt_geht_auf: int
    neu_geht_auf: int
    gewonnen: int
    verloren: int


def bilanz(befunde: list[Befund]) -> Bilanz:
    return Bilanz(
        gesamt=len(befunde),
        alt_geht_auf=sum(1 for b in befunde if b.alt_geht_auf),
        neu_geht_auf=sum(1 for b in befunde if b.neu_geht_auf),
        gewonnen=sum(1 for b in befunde if b.gewonnen),
        verloren=sum(1 for b in befunde if b.verloren),
    )


def neu_auswerten(db: Session, attachment_ids: list[int]) -> int:
    """Schreibt die Positionen der genannten Belege aus ihrem Rohtext neu.

    **Nur wo die neue Auswertung aufgeht.** Eine Aufteilung, die den Total nicht
    ergibt, ersetzt keine andere, die es auch nicht tut — dann bliebe es beim
    Raten, nur mit anderen Zahlen. Händler und Datum bleiben unangetastet: sie
    sind womöglich von Hand korrigiert, und der Rohtext weiss davon nichts.
    """
    if not attachment_ids:
        return 0
    geaendert = 0
    for att in db.scalars(select(Attachment).where(Attachment.id.in_(attachment_ids))):
        text = att.ocr_text or ""
        try:
            daten = json.loads(att.parsed_items_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(daten, dict):
            continue
        neu_items = parse_receipt_items_menge(text)
        neu_total = extract_amount(text)
        if not _geht_auf([p for _n, p, _m in neu_items], neu_total):
            continue
        daten["items"] = [
            {"name": name, "price": str(preis), "qty": menge, "category_id": None}
            for name, preis, menge in neu_items
        ]
        daten["amount"] = str(neu_total)
        daten["positions_ok"] = True
        att.parsed_items_json = json.dumps(daten, ensure_ascii=False)
        geaendert += 1
    db.commit()
    return geaendert
