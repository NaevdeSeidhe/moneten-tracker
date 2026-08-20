"""Auto-Zuordnung: Quittungen aus dem Ordner zu Buchungen vorschlagen.

Für jede noch nicht zugeordnete Quittung im Ordner wird versucht, die passende
Buchung zu finden:

* **Betrag** aus der Quittung (OCR / Text-Layer) vs. Buchungsbetrag (absolut).
* **Datum** aus dem Dateinamen vs. Buchungsdatum (Toleranz ± einige Tage).

Vorschläge werden gescort (Betrag- und Datums-Treffer); der beste Treffer wird
vorausgewählt. Der Nutzer bestätigt oder wählt manuell eine andere Buchung —
nichts wird automatisch ohne Bestätigung übernommen.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.db.models import ArchivedReceipt, Attachment, Transaction, not_transfer
from moneten.services import rechnungsbeleg
from moneten.services.attachments import ReceiptFile, list_receipts, resolve_receipt
from moneten.services.receipt_ocr import extract_text

# Datums-Toleranz für einen Treffer. Karten-Buchungen werden oft ein paar Tage später
# verbucht (Wochenende/Feiertage) — 7 Tage fängt die üblichen Verzögerungen ab.
_DATE_TOLERANCE_DAYS = 7

# Weites Datumsfenster für den Eindeutigkeits-Pass (Tier 2): per Überweisung bezahlte
# Rechnungen werden oft Wochen nach dem Rechnungsdatum verbucht und verfehlen das enge
# ±7-Tage-Fenster. So weit, dass die Zahlung noch reinfällt — aber begrenzt, damit
# zufällig betragsgleiche Buchungen aus ganz anderer Zeit nicht fälschlich greifen.
_WIDE_DATE_WINDOW_DAYS = 90

# Hinweis auf der Karte eines Belegs, der an keine Buchung gehört. Er nennt den
# Grund und die Handlung: der Assistent hat einen Archiv-Knopf, aber ohne diesen
# Satz sähe der Beleg wie einer aus, dem nur die passende Buchung fehlt.
KEIN_ZAHLUNGSBELEG = (
    "Verbindungsnachweis — weist Nutzung aus, keine Zahlung. Gehört an keine Buchung: archivieren."
)

# Wörter aus Dateinamen, die KEIN Händler sind (für die Händler-Stichwort-Erkennung).
_MERCHANT_STOP = {
    "rechnung", "beleg", "bestaetigung", "bestätigung", "jahresabo", "abo", "quittung",
    "kassenbon", "bon", "total", "chf", "mwst", "kopie", "scan", "img", "pdf", "fur",
    "und", "der", "die", "das", "von", "den", "ber",
}


@dataclass
class MatchCandidate:
    transaction: Transaction
    score: int
    amount_match: bool
    date_match: bool
    merchant_match: bool = False


@dataclass
class ReceiptSuggestion:
    receipt: ReceiptFile
    amount: Decimal | None      # aus OCR/Text-Layer geschätzt
    date_guess: date | None     # aus Dateiname oder OCR-Text
    best: Transaction | None
    candidates: list[Transaction]
    ocr_text: str = ""          # erkannter Text (für die Beleg-Vorschau im Assistenten)
    ocr_method: str = "none"    # "text-layer" | "ocr" | "none"
    reason: str | None = None   # warum kein Auto-Treffer (Diagnose-Hinweis auf der Karte)


def attach_receipt(db: Session, transaction: Transaction, filename: str) -> Attachment:
    """Legt eine Quittungs-Zuordnung an und liest den Beleg aus.

    Gemeinsame Logik für die manuelle Zuordnung (Buchungs-Tab) und den
    Auto-Zuordnungs-Assistenten. Kopiert nichts — referenziert nur.
    """
    matched = next((r for r in list_receipts() if r.name == filename), None)
    att = Attachment(
        transaction_id=transaction.id,
        file_path=matched.path if matched else None,
        original_name=filename,
    )
    db.add(att)
    db.flush()
    if att.file_path:
        resolved = resolve_receipt(att.file_path)
        if resolved is not None:
            read_receipt_data(att, str(resolved))
    return att


def read_receipt_data(att: Attachment, path: str) -> None:
    """Text und strukturierte Daten einer Beleg-Datei an den Anhang schreiben.

    **Eine Stelle für beide Belegarten.** Eine gelesene Rechnung liefert ihre
    Positionen (``rechnungsbeleg``), jeder andere Beleg den aus dem Text
    geschätzten Betrag. Hinge das an der Stelle, die den Anhang anlegt, dann
    stünden die Positionen nur an automatisch zugeordneten Belegen — und genau
    die von Hand zugeordnete Rechnung ist der Fall, den es gibt: bei mehreren
    passenden Buchungen ordnet die Automatik ausdrücklich NICHT zu.

    Aus demselben Grund steht hier auch „Text neu auslesen": der Knopf schrieb
    sonst die geprüften Positionen mit einem geschätzten Betrag zu.
    """
    result = extract_text(path)
    att.ocr_text = result.text or None
    daten = rechnungsbeleg.anhangs_daten(path, result.text)
    if daten is None:
        daten = {
            "method": result.method,
            "amount": str(result.amount) if result.amount is not None else None,
        }
    att.parsed_items_json = json.dumps(daten, ensure_ascii=False)


def unassigned_receipts(db: Session) -> list[ReceiptFile]:
    """Dateien im Quittungs-Ordner, die weder zugeordnet noch archiviert sind."""
    assigned = set(db.scalars(select(Attachment.original_name)))
    archived = set(db.scalars(select(ArchivedReceipt.filename)))
    done = assigned | archived
    return [r for r in list_receipts() if r.name not in done]


def earliest_transaction_date(db: Session) -> date | None:
    """Frühester Bank-Buchungstag — Quittungen davor haben keinen Bankeintrag."""
    return db.scalar(select(func.min(Transaction.date)))


def archive_receipt(db: Session, filename: str, *, reason: str = "manuell") -> bool:
    """Legt eine Quittung als „ohne Bankeintrag" ab (verschwindet aus der Liste).
    Gibt True zurück, wenn neu archiviert."""
    fn = (filename or "").strip()
    if not fn:
        return False
    if db.scalar(select(ArchivedReceipt).where(ArchivedReceipt.filename == fn)) is not None:
        return False
    db.add(ArchivedReceipt(filename=fn, reason=reason))
    db.commit()
    return True


_ARCHIVE_REASON_LABEL = {
    "vor-banktabelle": "vor dem ersten Bankeintrag (automatisch)",
    "ohne-bankbuchung": "keine Bankbuchung mit Betrag (Sammel-Archiv)",
    "manuell": "von Hand archiviert",
}


def archived_receipts(db: Session) -> list[dict]:
    """Alle archivierten Belege (Dateiname, Grund, ob die Datei noch im Ordner liegt) —
    für die „Archiv ansehen / reaktivieren"-Ansicht. Neueste zuerst."""
    present = {r.name for r in list_receipts()}
    rows = db.scalars(select(ArchivedReceipt).order_by(ArchivedReceipt.created_at.desc())).all()
    return [
        {
            "name": a.filename,
            "reason": a.reason or "—",
            "reason_label": _ARCHIVE_REASON_LABEL.get(a.reason or "", a.reason or "—"),
            "exists": a.filename in present,
        }
        for a in rows
    ]


def unarchive_receipt(db: Session, filename: str) -> bool:
    """Hebt die Archivierung eines Belegs auf (löscht den ArchivedReceipt-Eintrag) → der
    Beleg taucht wieder im Zuordnen-Assistenten auf. True, wenn etwas gelöscht wurde."""
    a = db.scalar(select(ArchivedReceipt).where(ArchivedReceipt.filename == (filename or "").strip()))
    if a is None:
        return False
    db.delete(a)
    db.commit()
    return True


def unarchive_all(db: Session, *, reason: str | None = None) -> int:
    """Reaktiviert alle archivierten Belege (optional nur die mit einem bestimmten
    ``reason``, z. B. nach einer Total-Korrektur die „ohne-bankbuchung"-Sammelablage,
    um sie neu abgleichen zu lassen). Gibt die Anzahl reaktivierter Belege zurück.

    Hinweis: Belege mit Datum **vor** dem ersten Bankeintrag legt der Assistent beim
    nächsten Öffnen automatisch wieder ab — die haben wirklich keine Buchung."""
    stmt = select(ArchivedReceipt)
    if reason is not None:
        stmt = stmt.where(ArchivedReceipt.reason == reason)
    rows = db.scalars(stmt).all()
    for a in rows:
        db.delete(a)
    db.commit()
    return len(rows)


def auto_archive_old(db: Session) -> int:
    """Archiviert automatisch alle Belege mit (Dateinamen-)Datum **vor** dem
    frühesten Bank-Buchungstag — die kann man keinem Bankeintrag zuordnen.

    Nutzt bewusst nur das Datum aus dem Dateinamen (kein teures OCR). Belege ohne
    erkennbares Datum bleiben für die manuelle Ablage. Gibt die Anzahl zurück.
    """
    cutoff = earliest_transaction_date(db)
    if cutoff is None:
        return 0
    count = 0
    for r in unassigned_receipts(db):
        if r.parsed_date is not None and r.parsed_date < cutoff and archive_receipt(db, r.name, reason="vor-banktabelle"):
            count += 1
    return count


def _attached_tx_ids(db: Session) -> set[int]:
    return set(db.scalars(select(Attachment.transaction_id)))


def _merchant_tokens(filename: str, text: str | None) -> tuple[str, ...]:
    """Händler-Stichwörter — **primär aus dem Dateinamen** (z. B. ``…_Migros_…``), denn
    der ist sauber. Nur wenn der Dateiname nichts hergibt (generisch wie ``IMG_1234``),
    wird die erste „echte" Belegzeile genutzt. Dient dazu, betrags-/datumsgleiche
    Buchungen über den Händler im Banktext eindeutig zu machen (``migros`` → nur die
    Migros-Buchung). Ortsnamen aus dem Belegtext (z. B. „Zug") werden so vermieden."""
    def _clean(words: list[str]) -> list[str]:
        out: list[str] = []
        for w in words:
            wl = w.lower()
            if wl not in _MERCHANT_STOP and wl not in out:
                out.append(wl)
        return out

    out = _clean(re.findall(r"[A-Za-zÄÖÜäöü]{3,}", filename))
    if not out:  # generischer Dateiname → Händler aus der ersten Belegzeile
        for line in (text or "").splitlines():
            if len(re.sub(r"[^A-Za-zÄÖÜäöü]", "", line.strip())) >= 3:
                out = _clean(re.findall(r"[A-Za-zÄÖÜäöü]{3,}", line))
                break
    return tuple(out[:5])


def _candidate_transactions(db: Session) -> list[Transaction]:
    """Kandidaten für die Beleg-Zuordnung: nur **Ausgaben** (Betrag < 0), keine Transfers.

    Umbuchungen/Bargeldbezüge nie als Beleg-Kandidat: eine Quittung gehört zu einem Kauf,
    nicht zu einer Geld-Verschiebung zwischen eigenen Konten. Und nie Gutschriften: der
    Betrags-Vergleich läuft über ``copy_abs()`` — ein Kaufbeleg über 51.00 würde sonst an
    einer betragsgleichen Gutschrift (z. B. der Rückerstattung desselben Kaufs) landen.
    """
    return list(db.scalars(select(Transaction).where(not_transfer(), Transaction.amount < 0)))


def _score_candidates(
    db: Session, amount: Decimal | None, date_guess: date | None,
    merchant_tokens: tuple[str, ...] = (),
    *, txs: list[Transaction] | None = None, attached: set[int] | None = None,
) -> list[MatchCandidate]:
    """Bewertet Buchungen ohne Anhang gegen Betrag/Datum (+ Händler) der Quittung.

    ``txs``/``attached`` können vorgeladen übergeben werden (Assistent scort bis zu
    50 Belege pro Seitenaufruf — ohne Hoisting wären das 50 Full-Table-Scans)."""
    if attached is None:
        attached = _attached_tx_ids(db)
    if txs is None:
        txs = _candidate_transactions(db)

    result: list[MatchCandidate] = []
    for t in txs:
        if t.id in attached:
            continue
        amount_match = amount is not None and t.amount.copy_abs() == amount
        date_match = False
        date_exact = False
        if date_guess is not None:
            delta = abs((t.date - date_guess).days)
            date_exact = delta == 0
            date_match = delta <= _DATE_TOLERANCE_DAYS
        desc = (t.description or "").lower()
        # Wortgrenzen (\b), damit z. B. „aldi" nicht in „RIVALDI SOLUTIONS" trifft — sonst
        # würde der Händler-Schutz (Tier 1 + Tier 2) durch Teilstring-Zufall ausgehebelt.
        merchant_match = bool(merchant_tokens) and any(
            re.search(rf"\b{re.escape(tok)}\b", desc) for tok in merchant_tokens
        )

        score = 0
        if amount_match:
            score += 3
        if date_exact:
            score += 2
        elif date_match:
            score += 1
        if merchant_match:
            score += 2
        if score > 0:
            result.append(MatchCandidate(t, score, amount_match, date_match, merchant_match))

    # Beste zuerst: höchster Score, dann geringste Datumsdistanz.
    def _date_distance(c: MatchCandidate) -> int:
        return abs((c.transaction.date - date_guess).days) if date_guess else 0

    result.sort(key=lambda c: (-c.score, _date_distance(c)))
    return result


def unassigned_log(db: Session) -> list[dict]:
    """Diagnose-Zeilen zu ALLEN unzugeordneten Belegen (Dateiname, Betrag, Datum,
    Quelle, Grund) — für den kopierbaren Export/Log. OCR-Ergebnisse kommen aus dem
    Cache (vom Auto-Abgleich), daher ohne erneutes Tesseract (``ocr=False``)."""
    out: list[dict] = []
    for r in unassigned_receipts(db):
        res = extract_text(r.path, ocr=False)
        date_guess = r.parsed_date or res.date
        scored = _score_candidates(db, res.amount, date_guess, _merchant_tokens(r.name, res.text))
        out.append({
            "name": r.name,
            "amount": str(res.amount) if res.amount is not None else "—",
            "date": date_guess.isoformat() if date_guess else "—",
            "method": res.method,
            "reason": _no_match_reason(db, res.amount, scored) or "matchbar (Vorschlag vorhanden)",
        })
    return out


def archivable_unmatched(db: Session) -> list[str]:
    """Dateinamen der Belege, die definitiv NICHT (auto) matchbar sind: kein Bankeintrag
    mit dem Betrag (Rechnung/Bargeld) ODER passende Buchung schon belegt (Duplikat).
    Bewusst NICHT die mit „Betrag passt, Datum >7 Tage" (manuell zuordenbar) und nicht
    die ohne erkannten Betrag (die könnten echte Belege sein)."""
    out: list[str] = []
    for r in unassigned_receipts(db):
        res = extract_text(r.path, ocr=False)
        if res.amount is None:
            continue
        scored = _score_candidates(db, res.amount, r.parsed_date or res.date, _merchant_tokens(r.name, res.text))
        reason = _no_match_reason(db, res.amount, scored) or ""
        if "Keine Bankbuchung" in reason or "schon einen Beleg" in reason:
            out.append(r.name)
    return out


def _no_match_reason(db: Session, amount: Decimal | None, scored: list[MatchCandidate]) -> str | None:
    """Erklärt, WARUM ein Beleg (noch) nicht automatisch zugeordnet wurde — als Hinweis
    auf der Karte. ``None`` heisst „ein klarer Vorschlag liegt vor" (kein Hinweis nötig)."""
    if amount is None:
        return "Kein Betrag erkannt — Beleg öffnen und manuell zuordnen"
    strong = [c for c in scored if c.amount_match and c.date_match]
    if len(strong) > 1:
        return "Mehrere Buchungen passen (Betrag + Datum) — bitte die richtige wählen"
    if strong:
        return None  # genau eine passende Buchung → Vorschlag ist vorausgewählt
    if any(c.amount_match for c in scored):
        return "Betrag passt, Zahlung aber weiter weg oder mehrdeutig — Betrag oben ins Suchfeld tippen und manuell zuordnen"
    has_attached = db.scalar(
        select(Attachment.id)
        .join(Transaction, Attachment.transaction_id == Transaction.id)
        .where(Transaction.amount.in_([amount, -amount]))
        .limit(1)
    )
    if has_attached:
        return "Passende Buchung hat schon einen Beleg (evtl. Duplikat)"
    return "Keine Bankbuchung mit diesem Betrag (Rechnung/Bargeld? → archivieren)"


def _unique_amount_winner(
    amount: Decimal | None, date_guess: date | None, scored: list[MatchCandidate],
    *, reliable_amount: bool,
) -> Transaction | None:
    """Tier-2-Auto-Treffer: genau EINE noch offene **Ausgabe** mit exakt diesem Betrag im
    weiten Datumsfenster (±:data:`_WIDE_DATE_WINDOW_DAYS`) — für per Überweisung bezahlte
    Rechnungen, deren Zahlung das enge ±7-Tage-Fenster verfehlt.

    Verknüpft aber nur bei **starker Evidenz**, sonst bleibt der Beleg für die manuelle
    Suche offen:
    * **Datum Pflicht** — begrenzt das Fenster (ohne Datum kein Tier-2-Treffer).
    * **Nur Ausgaben** (Betrag < 0); Umbuchungen sind schon aus den Kandidaten raus.
    * **Eindeutig** — mehr als eine betragsgleiche Buchung im Fenster → manuell.
    * Und entweder ein **krummer Cent-Betrag aus zuverlässiger Text-Quelle**
      (``reliable_amount`` = Text-Layer, kein OCR-Lesefehler möglich, geringe
      Kollisionsgefahr) **oder** der **Händlername** steht im Banktext. Reine
      Eindeutigkeit bei runden ODER OCR-gelesenen Beträgen ist zu unsicher
      (Fehl-Lesefehler, Zufallstreffer) → wird NICHT automatisch zugeordnet.
    """
    if amount is None or date_guess is None:
        return None
    in_window = [
        c for c in scored
        if c.amount_match
        and c.transaction.amount < 0  # Quittung → nur Ausgabe, nie Eingang
        and abs((c.transaction.date - date_guess).days) <= _WIDE_DATE_WINDOW_DAYS
    ]
    if len(in_window) != 1:
        return None  # 0 = nichts im Fenster, >1 = mehrdeutig → manuell
    cand = in_window[0]
    crooked_cents = amount % 1 != 0  # z. B. 464.20 → spezifisch, geringe Kollisionsgefahr
    if (crooked_cents and reliable_amount) or cand.merchant_match:
        return cand.transaction
    return None  # runder/OCR-Betrag ohne Händler-Bestätigung → zu unsicher für Auto


def find_match(
    db: Session, amount: Decimal | None, date_guess: date | None,
    *, merchant_tokens: tuple[str, ...] = (), reliable_amount: bool = True,
    allow_unique_amount: bool = True,
) -> Transaction | None:
    """**Gemeinsame** Auto-Zuordnungs-Logik für Ordner-Belege UND Foto-Scans.

    * Tier 1 — exakter Betrag **und** Datum in ±7 Tagen, eindeutig (bei mehreren über den
      Händler aufgelöst).
    * Tier 2 — exakter Betrag, **eindeutig** im weiten Datumsfenster (siehe
      :func:`_unique_amount_winner`); nur wenn ``allow_unique_amount``.

    ``reliable_amount``: Betrag aus zuverlässiger Quelle (Text-Layer)? Foto-/OCR-Belege
    übergeben ``False`` → Tier 2 greift dann nur mit Händler-Bestätigung (gegen Lesefehler).
    Gibt die eindeutig passende Buchung zurück oder ``None``.
    """
    if amount is None:
        return None
    scored = _score_candidates(db, amount, date_guess, merchant_tokens)
    winner: Transaction | None = None
    strong = [c for c in scored if c.amount_match and c.date_match]
    if len(strong) == 1:
        winner = strong[0].transaction
    elif len(strong) > 1:
        # Mehrere Betrag+Datum-Treffer → nur über GENAU einen Händler-Treffer auflösen.
        by_merchant = [c for c in strong if c.merchant_match]
        if len(by_merchant) == 1:
            winner = by_merchant[0].transaction
    if winner is None and allow_unique_amount:
        winner = _unique_amount_winner(amount, date_guess, scored, reliable_amount=reliable_amount)
    return winner


def auto_match_one(
    db: Session, receipt: ReceiptFile, *, ambiguous_amounts: set[Decimal] | None = None,
    cache: dict | None = None,
) -> bool:
    """Versucht, **eine** Ordner-Quittung automatisch zuzuordnen (Tier 1 + Tier 2 über
    :func:`find_match`). Datum aus Dateiname oder OCR-Text. Tier 2 wird gesperrt, wenn der
    Betrag bei mehreren offenen Belegen vorkommt (Reihenfolge-Zufall). True + commit bei
    Treffer; Pro-Beleg-Variante für den schrittweisen Hintergrund-Job.

    **Vor allen Schätzungen** greifen zwei Regeln aus dem Inhalt des Belegs
    (siehe ``rechnungsbeleg``): ein Verbindungsnachweis wird nie zugeordnet — er
    weist Nutzung aus, keine Zahlung —, und eine Anbieter-Rechnung wird über
    ihren gelesenen Rechnungsbetrag zugeordnet statt über einen geschätzten.
    """
    # ``cache`` ist der Grund, warum :func:`auto_match` mehrfach laufen DARF:
    # ohne ihn läse jeder Durchgang jeden offenen Beleg neu — bei Fotos heisst
    # das OCR, also Sekunden pro Datei. Mit ihm kostet ein zweiter Durchgang
    # praktisch nichts. ``None`` (= kein Treffer) muss dabei mit gemerkt werden,
    # sonst wäre genau der teure Fall der ungecachte.
    res = _mit_cache(cache, ("text", receipt.path), lambda: extract_text(receipt.path))
    # Rechnung ZUERST: ein Dokument, dessen Positionen auf den Rechnungsbetrag
    # aufgehen, ist eine Rechnung — was auch immer sonst darin vorkommt. Erst
    # wenn es keine ist, entscheidet der Titel über den Nutzungsbeleg.
    rechnung = _mit_cache(cache, ("rechnung", receipt.path),
                          lambda: rechnungsbeleg.rechnung_zur_datei(receipt.path, res.text))
    if rechnung is None and rechnungsbeleg.ist_verbindungsnachweis(res.text):
        return False
    if rechnung is not None:
        gelesen = rechnungsbeleg.passende_buchung(db, rechnung)
        if gelesen is None:
            # Kein eindeutiger Treffer. Der geschätzte Weg darunter darf es NICHT
            # noch einmal versuchen: er würde mit demselben Betrag und einer
            # weicheren Regel genau die Zuordnung treffen, die hier gerade als
            # nicht belegbar verworfen wurde.
            return False
        attach_receipt(db, gelesen, receipt.name)
        db.commit()
        return True
    if res.amount is None:
        return False
    date_guess = receipt.parsed_date or res.date  # Dateiname zuerst, dann Belegtext
    if ambiguous_amounts is None:
        ambiguous_amounts = _ambiguous_receipt_amounts(db)
    winner = find_match(
        db, res.amount, date_guess,
        merchant_tokens=_merchant_tokens(receipt.name, res.text),
        reliable_amount=res.method == "text-layer",
        allow_unique_amount=res.amount not in ambiguous_amounts,
    )
    if winner is None:
        return False
    attach_receipt(db, winner, receipt.name)
    db.commit()
    return True


def _ambiguous_receipt_amounts(db: Session) -> set[Decimal]:
    """Beträge, die bei MEHR ALS EINEM offenen Beleg vorkommen — günstig ermittelt
    (``ocr=False``, nutzt den OCR-Cache des Abgleichs). Solche Beträge sind für Tier-2
    gesperrt: bei zwei gleichbetragten Belegen ist nicht entscheidbar, welcher zur
    einzigen Buchung gehört (Reihenfolge-Zufall) → beide bleiben für die manuelle Suche."""
    counts: dict[Decimal, int] = {}
    for r in unassigned_receipts(db):
        amt = extract_text(r.path, ocr=False).amount
        if amt is not None:
            counts[amt] = counts.get(amt, 0) + 1
    return {amt for amt, n in counts.items() if n > 1}


# Hoechstzahl der Durchgaenge von :func:`auto_match`. Zwoelf Rechnungen eines
# Jahres brauchen im schlimmsten Fall zwoelf.
_MAX_LAEUFE = 15


def _mit_cache(cache: dict | None, schluessel: tuple, berechnen):
    """Wert aus dem Zwischenspeicher — oder einmal berechnen und ablegen."""
    if cache is None:
        return berechnen()
    if schluessel not in cache:
        cache[schluessel] = berechnen()
    return cache[schluessel]


def auto_match(db: Session) -> int:
    """Ordnet alle eindeutigen, sicheren Treffer automatisch zu (siehe
    :func:`auto_match_one`). Gibt die Anzahl zugeordneter Quittungen zurück."""
    # Mehrere Durchgaenge, bis keiner mehr etwas findet. Die Anbieter-Rechnungen
    # bilden eine KETTE: die des Februars darf die Januar-Buchung nur uebergehen,
    # wenn dort schon die Januar-Rechnung haengt (siehe
    # ``rechnungsbeleg.passende_buchung``). In einem einzigen Durchgang haengt es
    # damit an der Reihenfolge der Dateien, ob die Kette aufgeht.
    #
    # Die Schranke ist kein Zeitlimit, sondern ein Riegel: jeder Durchgang muss
    # etwas zugeordnet haben, sonst endet die Schleife von selbst. ``_MAX_LAEUFE``
    # faengt nur den Fall ab, dass sich zwei Regeln gegenseitig aufschaukeln.
    gesamt = 0
    cache: dict = {}
    for _ in range(_MAX_LAEUFE):
        ambiguous = _ambiguous_receipt_amounts(db)
        runde = sum(
            1 for receipt in unassigned_receipts(db)
            if auto_match_one(db, receipt, ambiguous_amounts=ambiguous, cache=cache)
        )
        gesamt += runde
        if not runde:
            break
    return gesamt


def build_suggestions(db: Session, limit: int = 50, *, ocr: bool = True) -> list[ReceiptSuggestion]:
    """Erzeugt Zuordnungs-Vorschläge für (bis zu ``limit``) unzugeordnete Quittungen.

    Datum aus Dateiname oder — als Fallback — aus dem OCR-Text. Der erkannte
    OCR-Text wird mitgegeben (für die Beleg-Vorschau im Assistenten).

    Mit ``ocr=False`` lädt die Liste **schnell** (kein Tesseract): Text-Layer-PDFs
    liefern weiter Betrag/Text, reine Foto-Belege erscheinen ohne Betrag und werden
    über den schrittweisen Hintergrund-Abgleich (``auto/begin``) verarbeitet. Das
    verhindert, dass die Übersicht bei vielen Foto-Belegen in einen Timeout läuft.

    Belege **mit erkanntem Betrag** (sinnvoll abgleichbar) werden zuerst gezeigt;
    Belege **ohne** Betrag (alte Scans ohne lesbaren Text) kommen ans Ende — und OHNE
    irreführende Vorauswahl: dafür lohnt nur „Beleg öffnen" + manuell zuordnen."""
    # Günstig (Text-Layer/Cache, kein teures Scoring): Betrag/Datum je Beleg bestimmen.
    extracted = [(r, extract_text(r.path, ocr=ocr)) for r in unassigned_receipts(db)[:200]]
    # Mit Betrag zuerst, dann neueste zuerst.
    extracted.sort(key=lambda rr: (rr[1].amount is None, -(rr[0].parsed_date or date.min).toordinal()))

    # Kandidaten + belegte IDs EINMAL laden (statt einmal pro Beleg = 50 Full-Scans).
    txs = _candidate_transactions(db)
    attached = _attached_tx_ids(db)

    suggestions: list[ReceiptSuggestion] = []
    for receipt, res in extracted[:limit]:
        date_guess = receipt.parsed_date or res.date
        # Rechnung ZUERST — dieselbe Reihenfolge wie in :func:`auto_match_one`.
        # Sie stand dort und hier nicht: eine Rechnung, die den Begriff nur
        # erwähnt, bekam im Assistenten „kein Zahlungsbeleg" und den Rat, sie
        # wegzuräumen. Und zwar genau dann, wenn die Automatik sie NICHT
        # zugeordnet hat — also im einzigen Moment, in dem der Assistent zählt.
        ist_rechnung = rechnungsbeleg.rechnung_zur_datei(receipt.path, res.text) is not None
        if not ist_rechnung and rechnungsbeleg.ist_verbindungsnachweis(res.text):
            # Auch keine Vorauswahl: der Assistent legt den besten Kandidaten
            # vor und der Nutzer bestätigt ihn mit einem Klick. Für einen Beleg,
            # der an KEINE Buchung gehört, wäre jeder Vorschlag ein Fehltritt,
            # den er nur noch abnicken muss.
            suggestions.append(ReceiptSuggestion(
                receipt=receipt, amount=res.amount, date_guess=date_guess, best=None,
                candidates=[], ocr_text=(res.text or "")[:1500], ocr_method=res.method,
                reason=KEIN_ZAHLUNGSBELEG,
            ))
            continue
        if res.amount is None:
            # Ohne Betrag keine (raterische) Vorauswahl — nur ansehen + manuell.
            suggestions.append(ReceiptSuggestion(
                receipt=receipt, amount=None, date_guess=date_guess, best=None,
                candidates=[], ocr_text=(res.text or "")[:1500], ocr_method=res.method,
                reason=_no_match_reason(db, None, []),
            ))
            continue
        scored = _score_candidates(db, res.amount, date_guess, _merchant_tokens(receipt.name, res.text),
                                   txs=txs, attached=attached)
        suggestions.append(ReceiptSuggestion(
            receipt=receipt,
            amount=res.amount,
            date_guess=date_guess,
            best=scored[0].transaction if scored else None,
            candidates=[c.transaction for c in scored[:8]],
            ocr_text=(res.text or "")[:1500],
            ocr_method=res.method,
            reason=_no_match_reason(db, res.amount, scored),
        ))
    return suggestions


def build_suggestion_for(db: Session, filename: str) -> ReceiptSuggestion | None:
    """Baut den Vorschlag für EINEN (wieder offenen) Beleg — für den Re-Render nach
    dem „Rückgängig". None, wenn die Datei nicht (mehr) unzugeordnet ist."""
    rec = next((r for r in unassigned_receipts(db) if r.name == filename), None)
    if rec is None:
        return None
    res = extract_text(rec.path, ocr=False)
    date_guess = rec.parsed_date or res.date
    # Rechnung zuerst, siehe :func:`build_suggestions`.
    ist_rechnung = rechnungsbeleg.rechnung_zur_datei(rec.path, res.text) is not None
    if not ist_rechnung and rechnungsbeleg.ist_verbindungsnachweis(res.text):
        return ReceiptSuggestion(
            receipt=rec, amount=res.amount, date_guess=date_guess, best=None,
            candidates=[], ocr_text=(res.text or "")[:1500], ocr_method=res.method,
            reason=KEIN_ZAHLUNGSBELEG,
        )
    scored = _score_candidates(db, res.amount, date_guess, _merchant_tokens(rec.name, res.text))
    return ReceiptSuggestion(
        receipt=rec,
        amount=res.amount,
        date_guess=date_guess,
        best=scored[0].transaction if scored else None,
        candidates=[c.transaction for c in scored[:8]],
        ocr_text=(res.text or "")[:1500],
        ocr_method=res.method,
        reason=_no_match_reason(db, res.amount, scored),
    )
