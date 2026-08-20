"""Preisverlauf einzelner Artikel aus gescannten Quittungen.

Die Budget-Seite zeigt, dass „Lebensmittel" teurer geworden sind. Sie sagt aber
nicht, ob das an höheren Preisen oder an mehr Einkäufen liegt. Diese Auswertung
beantwortet genau das — aus Daten, die ohnehin schon in der App liegen: den
Positionen der digitalisierten Belege.

Kein Vergleich mit fremden Läden, keine Anbindung an Preisportale. Es geht nur
um die eigene Historie: *was habe ich für diesen Artikel früher bezahlt?*

Die heikle Stelle ist die Zuordnung „ist das derselbe Artikel?". Zwei bewusste
Festlegungen dazu:

* **Im Zweifel trennen, nicht zusammenfassen.** Zwei getrennte Reihen für
  denselben Artikel sind ärgerlich. Zwei verschiedene Artikel in einer Reihe
  erzeugen einen erfundenen Preissprung — das ist schlimmer, weil es wie eine
  Erkenntnis aussieht. Deshalb bleibt die Gebindegrösse Teil des Schlüssels:
  „Butter 250g" und „Butter 500g" sind nicht derselbe Artikel.
* **Stückzahl wird herausgerechnet, Gebindegrösse nicht.** Der Beleg-Parser
  liefert das Positions-*Total*; zwei Butter kosten dort das Doppelte. Ohne
  Division sähe jeder Vorratseinkauf wie eine Preiserhöhung aus.

Grenze, die bleibt: erkennt der Beleg-Parser eine Stückzahl nicht (weil sie in
einer eigenen Spalte stand, die die OCR verschluckt hat), zeigt die Reihe dort
einen Ausreisser. Deshalb steht neben jedem Punkt das Datum — ein einzelner
Ausschlag ist so als solcher erkennbar.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Attachment, PendingReceipt, Transaction

# Stückzahl: „2 x", „2x", „2 ST", „3 Stk". Wird herausgerechnet.
_STUECKZAHL = re.compile(r"\b(\d{1,2})\s*(?:x|st|stk|stück|stueck)\b", re.IGNORECASE)
# Gebindegrösse mit Leerzeichen („250 g") auf eine Schreibweise ziehen („250g").
_GEBINDE = re.compile(r"\b(\d+)\s+(kg|g|ml|dl|cl|l)\b", re.IGNORECASE)


@dataclass
class Punkt:
    """Eine Beobachtung: an diesem Tag kostete der Artikel so viel."""

    datum: date
    preis: Decimal
    haendler: str = ""
    name: str = ""      # Schreibweise auf genau diesem Beleg


@dataclass
class Artikel:
    """Ein Artikel mit allen Beobachtungen, chronologisch."""

    schluessel: str
    name: str
    punkte: list[Punkt] = field(default_factory=list)

    @property
    def erst(self) -> Decimal:
        return self.punkte[0].preis

    @property
    def letzt(self) -> Decimal:
        return self.punkte[-1].preis

    @property
    def diff(self) -> Decimal:
        return self.letzt - self.erst

    @property
    def pct(self) -> float:
        return float(self.diff / self.erst * 100) if self.erst > 0 else 0.0

    @property
    def guenstigster(self) -> Punkt:
        return min(self.punkte, key=lambda p: p.preis)


# Zahlarten sind keine Artikel. Der Beleg-Parser filtert sie beim Scannen, aber
# schon gespeicherte Belege tragen sie noch — ohne diesen zweiten Filter muesste
# man alles neu einlesen, um einen falschen Eintrag loszuwerden.
_KEIN_ARTIKEL = re.compile(
    r"(?<![a-zäöü])(?:[vu][i1l]sa|ma[e3]s?tro|mastercard|postfinance|amex|twint|"
    r"kreditkarte|debit|rückgeld|ruckgeld|trinkgeld|total|zwischensumme|"
    # Dieselben Summen-Wortlaute wie in ``receipt_split._SKIP``. Das Tor dort
    # hält sie ab heute vom Beleg fern — hier stehen aber die Positionen, die
    # VORHER durchgingen und längst gespeichert sind. Ein „Artikel", dessen
    # Preis das Belegtotal ist, sieht im Verlauf wie Teuerung aus.
    r"gesamt\w*|(?:end|zahl|rechnungs|schluss|gesamt)betrag|be?zahl(?:t|en)|"
    r"summe|endsumme)(?![a-zäöü])",
    re.IGNORECASE,
)


def ist_artikel(name: str) -> bool:
    """Falsch fuer Zahlarten und Beleg-Summen, die als Position gelesen wurden."""
    return not _KEIN_ARTIKEL.search(name or "")


def artikel_schluessel(name: str) -> str:
    """Normalisiert einen Positionsnamen zu einem Vergleichs-Schlüssel.

    Wörter alphabetisch sortiert, damit „Bio Butter" und „Butter Bio" zusammen-
    finden. Die Gebindegrösse bleibt erhalten (siehe Modul-Docstring).
    """
    low = _STUECKZAHL.sub(" ", name.lower())
    low = re.sub(r"[^0-9a-zà-ÿ]+", " ", low)
    low = _GEBINDE.sub(r"\1\2", low)
    toks = [t for t in low.split() if len(t) >= 2 and not t.isdigit()]
    return " ".join(sorted(set(toks)))


def _stueckzahl(eintrag: dict, name: str) -> int:
    """Anzahl Stück dieser Position — 1, wenn keine bekannt ist.

    Erste Wahl ist das Feld ``qty``, das der Beleg-Parser beim Scannen mitschreibt.
    Es ist die einzige verlässliche Quelle: der gespeicherte Anzeigename hat die
    Menge nicht mehr, ``_clean_name`` entfernt sie für die Lesbarkeit.

    Der Rückfall auf den Namen ist für Belege da, die vor dieser Änderung gescannt
    wurden — dort fehlt ``qty``. Er trifft selten, schadet aber nie.
    """
    roh = eintrag.get("qty")
    if roh is not None:
        try:
            n = int(roh)
        except (ValueError, TypeError):
            n = 1
        if 1 <= n <= 50:
            return n
    m = _STUECKZAHL.search(name)
    if not m:
        return 1
    try:
        n = int(m.group(1))
    except ValueError:
        return 1
    return n if 1 <= n <= 50 else 1


def _preis(eintrag: dict, name: str) -> Decimal | None:
    """Positions-Total → Stückpreis."""
    try:
        total = Decimal(str(eintrag.get("price")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if total <= 0:
        return None
    return (total / _stueckzahl(eintrag, name)).quantize(Decimal("0.01"))


def _positionen(db: Session) -> list[tuple[str, Decimal, date, str]]:
    """Alle Belegpositionen als ``(name, stückpreis, datum, händler)``.

    Zwei Quellen: an Buchungen hängende Belege (:class:`Attachment`) und noch
    nicht zugeordnete (:class:`PendingReceipt`). Beim Attachment gilt das Datum
    der **Buchung** — das ist die bestätigte Angabe; das OCR-Datum im JSON ist
    nur geraten.
    """
    raw: list[tuple[str, Decimal, date, str]] = []

    tx_datum = {
        t.id: t.date for t in db.scalars(select(Transaction).where(Transaction.id.isnot(None)))
    }
    for att in db.scalars(select(Attachment).where(Attachment.parsed_items_json.isnot(None))):
        datum = tx_datum.get(att.transaction_id)
        try:
            strukt = json.loads(att.parsed_items_json or "{}")
        except (ValueError, TypeError):
            continue
        if datum is None:
            roh_datum = strukt.get("date")
            try:
                datum = date.fromisoformat(roh_datum) if roh_datum else None
            except ValueError:
                datum = None
        if datum is None:
            continue
        haendler = strukt.get("merchant") or ""
        for it in strukt.get("items", []):
            name = (it.get("name") or "").strip()
            preis = _preis(it, name)
            if name and preis is not None and ist_artikel(name):
                raw.append((name, preis, datum, haendler))

    for pend in db.scalars(select(PendingReceipt).where(PendingReceipt.items_json.isnot(None))):
        if pend.receipt_date is None:
            continue
        try:
            strukt = json.loads(pend.items_json or "{}")
        except (ValueError, TypeError):
            continue
        haendler = pend.merchant or strukt.get("merchant") or ""
        for it in strukt.get("items", []):
            name = (it.get("name") or "").strip()
            preis = _preis(it, name)
            if name and preis is not None and ist_artikel(name):
                raw.append((name, preis, pend.receipt_date, haendler))

    return raw


def preisverlauf(db: Session, *, min_punkte: int = 2, limit: int | None = None) -> list[Artikel]:
    """Artikel mit mindestens ``min_punkte`` Beobachtungen, teuerste Entwicklung zuerst.

    Unter zwei Punkten gibt es keinen Verlauf — ein einzelner Preis ist keine
    Aussage über Teuerung.
    """
    gruppen: dict[str, Artikel] = {}
    for name, preis, datum, haendler in _positionen(db):
        key = artikel_schluessel(name)
        if not key:
            continue
        art = gruppen.get(key)
        if art is None:
            art = gruppen[key] = Artikel(schluessel=key, name=name)
        art.punkte.append(Punkt(datum=datum, preis=preis, haendler=haendler, name=name))

    ergebnis = []
    for art in gruppen.values():
        art.punkte.sort(key=lambda p: p.datum)
        # Mehrere Positionen desselben Tages (z.B. zwei Zeilen auf einem Beleg)
        # sind eine Beobachtung, kein Verlauf.
        if len({p.datum for p in art.punkte}) < min_punkte:
            continue
        # Anzeigename: die jüngste Schreibweise — sie passt zum aktuellen Beleg.
        art.name = art.punkte[-1].name or art.name
        ergebnis.append(art)

    ergebnis.sort(key=lambda a: (a.pct, a.diff), reverse=True)
    return ergebnis[:limit] if limit else ergebnis
