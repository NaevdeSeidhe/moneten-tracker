"""Automatische Kategorisierung von Buchungen über Stichwort-Regeln.

Eine Regel (``CategoryRule``) ordnet einer Kategorie ein Stichwort zu. Enthält
der Buchungstext das Stichwort (Teilstring, case-insensitiv), wird die Buchung
dieser Kategorie zugewiesen. **Erste passende Regel gewinnt** (Reihenfolge über
``sort_order``). Manuell gesetzte Kategorien werden **nie** überschrieben.

Datenschutz: Die App liefert nur die Engine + ein generisches CH-Starter-Set.
Eigene Händler trägt der Nutzer selbst ein — niemand muss die Buchungen lesen.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.db.models import (
    Category,
    CategoryRule,
    ManagementType,
    Transaction,
    enthaelt,
    not_transfer,
)


def load_active_rules(db: Session) -> list[tuple[str, int]]:
    """Aktive Regeln als (keyword_lower, category_id), in Prüf-Reihenfolge."""
    rules = db.scalars(
        select(CategoryRule).where(CategoryRule.is_active.is_(True))
        .order_by(CategoryRule.sort_order, CategoryRule.id)
    )
    return [(r.keyword.lower().strip(), r.category_id) for r in rules if r.keyword and r.keyword.strip()]


def match_category(pairs: list[tuple[str, int]], description: str | None) -> int | None:
    """Erste Regel, deren Stichwort im Text vorkommt → category_id (sonst None)."""
    text = (description or "").lower()
    for keyword, category_id in pairs:
        if keyword in text:
            return category_id
    return None


def transfer_category_ids(db: Session) -> set[int]:
    """IDs aller Kategorien, deren Top-Kategorie ``management_type=TRANSFER`` ist.

    Buchungen, die in eine solche Kategorie fallen (z.B. „Bargeldbezug"), sind
    Umbuchungen — kein echter Aufwand. Sie werden beim Kategorisieren zusätzlich
    als ``management_type=TRANSFER`` markiert, damit sie aus Ausgaben/Budget/Sankey
    herausfallen.
    """
    # Alle Kategorien einmal als {id: Kategorie}-Dict laden → schnelles Nachschlagen
    # der Eltern, ohne pro Kategorie erneut die DB zu fragen.
    cats = {c.id: c for c in db.scalars(select(Category))}

    def _top_mgmt(c: Category):
        # Von der Kategorie nach oben zur Wurzel laufen (parent_id == None) und
        # deren management_type zurückgeben. ``seen`` ist nur ein Sicherheitsnetz
        # gegen (eigentlich unmögliche) Zyklen, damit die Schleife immer endet.
        seen: set[int] = set()
        while c.parent_id is not None and c.parent_id in cats and c.id not in seen:
            seen.add(c.id)
            c = cats[c.parent_id]
        return c.management_type

    return {cid for cid, c in cats.items() if _top_mgmt(c) == ManagementType.TRANSFER}


def apply_rules(db: Session, *, only_uncategorized: bool = True) -> int:
    """Wendet alle aktiven Regeln auf bestehende Buchungen an. Gibt die Anzahl der
    geänderten Buchungen zurück.

    * Nur Top-Level-Buchungen (keine Split-Children), keine Transfers.
    * ``only_uncategorized=True`` lässt bereits kategorisierte Buchungen unberührt
      (manuelle Zuordnung bleibt erhalten).
    """
    pairs = load_active_rules(db)
    if not pairs:
        return 0
    transfer_ids = transfer_category_ids(db)
    q = select(Transaction).where(not_transfer())
    if only_uncategorized:
        q = q.where(Transaction.category_id.is_(None))

    changed = 0
    for tx in db.scalars(q):
        cat_id = match_category(pairs, tx.description)
        if cat_id is not None and tx.category_id != cat_id:
            tx.category_id = cat_id
            # Bargeldbezug & Co. zusätzlich als Transfer markieren (kein Aufwand).
            if cat_id in transfer_ids:
                tx.management_type = ManagementType.TRANSFER
            changed += 1
    db.commit()
    return changed


# ---------------------------------------------------------------------------
# Lernen aus manueller Kategorisierung
# ---------------------------------------------------------------------------

# Worte, die in Bank-Buchungstexten häufig vorne stehen, aber keinen Händler
# bezeichnen — als Stichwort-Vorschlag ungeeignet.
_NOISE_WORDS = {
    "gutschrift", "einkauf", "kauf", "zahlung", "lastschrift", "belastung",
    "twint", "überweisung", "ueberweisung", "vergütung", "verguetung",
    "dauerauftrag", "rechnung", "debitkarte", "kartenzahlung", "karte",
    "online", "kauf/dienstl", "kauf/dienstleistung", "der", "die", "das",
    "und", "für", "fuer", "von", "vom", "auftrag", "betrag",
    # Nachgemessen ergaenzt: ohne diese wurde „banking" allein zum Gruppen-
    # schluessel und warf eine Sozialversicherung, eine Steuerverwaltung, einen
    # Abo-Dienst und eine nackte IBAN in denselben Topf — eine Gruppe, die man
    # nicht am Stueck zuweisen kann.
    "banking", "e-banking", "ebanking", "ebill",
    # Die Kartenmarke steht in fast jedem Buchungstext und frass zwei von drei
    # Schluesselwoertern: „Einkauf Buchhandlung … Visa Debit-Nr." ergab
    # „buchhandlung visa debit" statt „buchhandlung".
    "visa", "debit", "mastercard", "maestro", "nr",
}


def suggest_keyword(description: str | None) -> str:
    """Schlägt ein Stichwort für eine Lern-Regel aus dem Buchungstext vor.

    Heuristik: erstes „bedeutungstragendes" Wort (≥3 Zeichen, kein Füllwort).
    Der Vorschlag ist nur ein Default — der Nutzer kann ihn im Formular ändern.
    """
    words = [w for w in re.findall(r"[0-9A-Za-zÀ-ÿ&]+", description or "") if len(w) >= 3]
    for w in words:
        if w.lower() not in _NOISE_WORDS:
            return w.lower()
    return words[0].lower() if words else ""


def learn_from_transaction(
    db: Session, *, keyword: str, category_id: int, source_tx_id: int | None = None
) -> tuple[bool, int]:
    """Lernt aus einer manuell bestätigten Kategorie.

    1. Legt — falls noch keine aktive Regel mit diesem Stichwort existiert — eine
       neue Regel Stichwort→Kategorie an (bzw. aktualisiert eine bestehende auf
       die neu bestätigte Kategorie).
    2. Ordnet alle weiteren **unkategorisierten** Top-Level-Buchungen, deren Text
       das Stichwort enthält, derselben Kategorie zu (Bargeldbezug & Co. zusätzlich
       als Transfer).

    Gibt ``(regel_neu_angelegt, anzahl_weiterer_zugeordneter)`` zurück. Wird vom
    Buchungs-Router nach einer manuellen Kategorisierung aufgerufen — läuft komplett
    serverseitig, ohne externe Hilfe.
    """
    kw = (keyword or "").lower().strip()
    if not kw or category_id is None or db.get(Category, category_id) is None:
        return (False, 0)

    existing = db.scalar(
        select(CategoryRule).where(
            CategoryRule.is_active.is_(True), func.lower(CategoryRule.keyword) == kw
        )
    )
    rule_created = False
    if existing is None:
        # sort_order < Starter-Set (100): die vom Nutzer bestätigte Zuordnung
        # gewinnt gegenüber generischen Regeln bei künftigen Importen.
        db.add(CategoryRule(keyword=kw, category_id=category_id, sort_order=80))
        rule_created = True
    elif existing.category_id != category_id:
        existing.category_id = category_id

    transfer_ids = transfer_category_ids(db)
    is_transfer = category_id in transfer_ids
    q = select(Transaction).where(
        Transaction.category_id.is_(None),
        not_transfer(),
        enthaelt(Transaction.description, kw),
    )
    applied = 0
    for tx in db.scalars(q):
        if source_tx_id is not None and tx.id == source_tx_id:
            continue
        tx.category_id = category_id
        if is_transfer:
            tx.management_type = ManagementType.TRANSFER
        applied += 1
    db.commit()
    return (rule_created, applied)


# ---------------------------------------------------------------------------
# Schnell-Zuordnen-Inbox: unkategorisierte Buchungen nach Händler gruppieren
# ---------------------------------------------------------------------------


@dataclass
class UncategorizedGroup:
    """Eine Gruppe gleichartiger, noch nicht kategorisierter Buchungen.

    ``keyword`` ist das aus dem Buchungstext abgeleitete Händler-Stichwort, über
    das die Gruppe (und später die Lern-Regel) gebildet wird.
    """

    keyword: str
    label: str                          # repräsentative Beschreibung
    count: int
    total: Decimal                      # Netto-Summe MIT Vorzeichen
    suggested_category_id: int | None
    suggested_category_name: str | None
    transactions: list                  # Einzelbuchungen [{id, date, desc, amount}] (gekappt)
    # Ein- UND Ausgänge in derselben Gruppe. Steht als letztes Feld, weil ein
    # Vorgabewert in einer Dataclass keine Felder ohne Vorgabe hinter sich duldet.
    gemischt: bool = False


def uncategorized_groups(db: Session, *, limit: int = 40) -> list[UncategorizedGroup]:
    """Gruppiert unkategorisierte Buchungen nach erkanntem Händler-Stichwort.

    Für die „Schnell-Zuordnen"-Inbox: statt jede Buchung einzeln zu bearbeiten,
    fasst diese Funktion gleichartige Buchungen (gleicher Händler) zusammen und
    schlägt eine Kategorie vor. Eine Bestätigung ordnet die ganze Gruppe zu und
    legt zugleich eine Lern-Regel an (siehe :func:`learn_from_transaction`).

    Kategorie-Vorschlag: (1) greift eine bestehende Regel auf den Text → deren
    Kategorie; (2) sonst die häufigste Kategorie bereits kategorisierter Buchungen
    mit demselben Stichwort; (3) sonst keiner. Buchungen ohne brauchbares Stichwort
    (z.B. ohne Beschreibung) werden ausgelassen — die ordnet man einzeln zu.
    """
    rows = db.scalars(
        select(Transaction).where(
            Transaction.category_id.is_(None),
            Transaction.is_split.is_(False),  # aufgeteilte Buchungen sind bereits zugeordnet
            not_transfer(),
        )
    ).all()
    pairs = load_active_rules(db)
    cat_names = {c.id: c.name for c in db.scalars(select(Category))}

    # Bereits kategorisierte Buchungen EINMAL laden → Vorschlag (2) ohne N+1-Queries.
    categorized = db.execute(
        select(Transaction.description, Transaction.category_id).where(
            Transaction.category_id.is_not(None), not_transfer()
        )
    ).all()
    cat_lower = [((d or "").lower(), cid) for d, cid in categorized]

    buckets: dict[str, list[Transaction]] = {}
    for tx in rows:
        key = suggest_keyword(tx.description)
        if not key:
            continue
        buckets.setdefault(key, []).append(tx)

    groups: list[UncategorizedGroup] = []
    for key, txs in buckets.items():
        descs = Counter((t.description or "").strip() for t in txs if (t.description or "").strip())
        label = descs.most_common(1)[0][0] if descs else key
        # Netto statt Summe der Betraege: eine Gruppe mit +1'000 und -200 zeigte
        # sonst 1'200 — eine Zahl, die es nicht gibt.
        total = sum((t.amount for t in txs), Decimal("0"))
        # Gemischte Gruppen sind genau die, die man NICHT am Stueck zuweisen
        # darf. Das muss die Kopfzeile sagen, nicht erst der Schaden danach.
        gemischt = any(t.amount > 0 for t in txs) and any(t.amount < 0 for t in txs)

        suggested = match_category(pairs, label)
        if suggested is None:
            matches = Counter(cid for d, cid in cat_lower if key in d)
            if matches:
                suggested = matches.most_common(1)[0][0]

        # Mehr Einzelbuchungen verfügbar machen → die Gruppen-Suche/Mehrfachauswahl
        # (Inbox) findet auch ältere Treffer, nicht nur die neuesten paar.
        sample = sorted(txs, key=lambda t: t.date, reverse=True)[:80]
        groups.append(
            UncategorizedGroup(
                keyword=key,
                label=label,
                count=len(txs),
                total=total,
                gemischt=gemischt,
                suggested_category_id=suggested,
                suggested_category_name=cat_names.get(suggested) if suggested else None,
                transactions=[
                    {"id": t.id, "date": t.date, "desc": t.description or "(ohne Beschreibung)", "amount": t.amount}
                    for t in sample
                ],
            )
        )

    # Grösste Gruppen zuerst: maximaler Effekt pro Klick.
    # Nach Betrag SORTIEREN heisst hier: nach Volumen. Mit Vorzeichen
    # landeten alle Eingänge am Ende, obwohl sie genauso dringend sind.
    groups.sort(key=lambda g: (-g.count, -abs(g.total)))
    return groups[:limit]


# ---------------------------------------------------------------------------
# Generisches CH-Starter-Regelset (auf existierende Seed-Kategorien gemappt).
# Eigene/persönliche Händler (Arbeitgeber, Vermieter, Lieblingsshops) trägt der
# Nutzer selbst nach.
# ---------------------------------------------------------------------------
_STARTER: list[tuple[str, str]] = [
    # Lebensmittel
    ("coop", "Lebensmittel"), ("migros", "Lebensmittel"), ("denner", "Lebensmittel"),
    ("aldi", "Lebensmittel"), ("lidl", "Lebensmittel"), ("volg", "Lebensmittel"),
    # Auswärts essen
    ("mcdonald", "Auswärts essen privat"), ("burger king", "Auswärts essen privat"),
    ("kfc", "Auswärts essen privat"), ("starbucks", "Kaffee"),
    # Kommunikation / Internet
    ("swisscom", "Internet / TV"), ("salt", "Handy-Abo"), ("sunrise", "Handy-Abo"),
    ("wingo", "Handy-Abo"), ("yallo", "Handy-Abo"),
    # Streaming
    ("netflix", "Streaming"), ("spotify", "Streaming"), ("disney", "Streaming"),
    # Mobilität
    ("sbb", "Einzelfahrten"), ("zvv", "Einzelfahrten"), ("postauto", "Einzelfahrten"),
    # Krankenkasse
    ("css", "Krankenkasse Grund (KVG)"), ("helsana", "Krankenkasse Grund (KVG)"),
    ("swica", "Krankenkasse Grund (KVG)"), ("sanitas", "Krankenkasse Grund (KVG)"),
    ("concordia", "Krankenkasse Grund (KVG)"), ("visana", "Krankenkasse Grund (KVG)"),

    # Hier stand einmal ein Block „Aus der Inbox zugeordnet": rund fuenfzehn
    # Haendler, die beim Betreiber aufgetaucht waren — Hochschule, Spenden-
    # empfaenger, Laeden, eine Ferienunterkunft, der Wohnkanton. Er ist entfernt,
    # aus zwei Gruenden.
    #
    # **Er verriet einen Haushalt.** Einzeln harmlos, zusammen ein Profil:
    # Ausbildung, Weltanschauung, Wohnort, Konsum.
    #
    # **Und er ordnete bei anderen falsch zu.** Jede Regel greift auf einen
    # Teiltreffer im Buchungstext. Ein fremder Laden mit aehnlichem Namen landete
    # still in der falschen Kategorie — und still falsch ist die schlechteste
    # Sorte falsch.
    #
    # Eigene Haendler traegt man in der App unter „Regeln" nach. Dort gehoeren
    # sie hin: sie sind Daten, keine Funktion.
]


def seed_starter_rules(db: Session) -> int:
    """Legt das CH-Starter-Regelset an, falls noch keine Regeln existieren.

    Idempotent: tut nichts, wenn bereits Regeln vorhanden sind.
    """
    if db.scalar(select(CategoryRule.id).limit(1)) is not None:
        return 0
    # **Nur Unterkategorien, und nur nicht archivierte.** Gebucht wird laut
    # Datenmodell auf der Unterkategorie; die Oberkategorie ist eine Klammer.
    # Ohne Filter entschied die Reihenfolge der Abfrage: legt jemand eine eigene
    # OBERkategorie „Lebensmittel" an, konnte eine Starter-Regel auf sie zeigen —
    # Buchungen landeten dann still an einer Stelle, an der keine landen soll.
    # Ebenso konnte eine archivierte Kategorie Ziel werden, obwohl sie in keiner
    # Auswahl mehr auftaucht. Gleiche Form wie in ``seeds.py``.
    cats = {
        c.name: c.id
        for c in db.scalars(
            select(Category).where(
                Category.parent_id.is_not(None), Category.is_archived.is_(False)
            )
        )
    }
    created = 0
    order = 100
    for keyword, cat_name in _STARTER:
        cat_id = cats.get(cat_name)
        if cat_id is None:
            continue
        db.add(CategoryRule(keyword=keyword, category_id=cat_id, sort_order=order))
        order += 10
        created += 1
    db.commit()
    return created
