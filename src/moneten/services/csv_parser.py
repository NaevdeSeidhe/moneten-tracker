"""CSV-Import als Fallback zum CAMT.053 (viele Banken bieten beides an).

Liefert bewusst dieselben Datenstrukturen wie der CAMT-Parser
(:class:`Camt053Statement` / :class:`Camt053Entry`), damit der Import-Router
CSV und CAMT identisch behandeln kann (Dedup, Saldo-Abgleich,
Auto-Kategorisierung).

Robust gegen die üblichen Schweizer Eigenheiten:
* **Trennzeichen** wird automatisch erkannt (``;``, ``,`` oder Tab).
* **Spalten** werden über Schlüsselwörter im Header erkannt (Datum/Betrag/Text/
  Saldo), mit positionsbasiertem Fallback. Auch getrennte
  ``Belastung``/``Gutschrift``-Spalten werden unterstützt.
* **Beträge** im Format ``1'234.50`` (Apostroph-Tausender), ``1234,50`` oder
  ``1.234,50`` werden korrekt nach ``Decimal`` geparst.
* **Datum** als ``31.05.2026`` oder ``2026-05-31``.

Nur Standard-Library — passt zur Offline-First-Vorgabe.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from moneten.services.camt053_parser import Camt053Entry, Camt053Statement

# Schlüsselwörter je Spaltentyp (lowercase, Teilstring-Match auf Header-Zellen).
_COL_DATE = ("buchungsdatum", "datum", "date", "valuta")
_COL_AMOUNT = ("betrag", "amount", "umsatz")
_COL_DEBIT = ("belastung", "soll", "debit", "lastschrift")
_COL_CREDIT = ("gutschrift", "haben", "credit")
_COL_DESC = ("buchungstext", "beschreibung", "text", "mitteilung", "description", "buchung")
_COL_BALANCE = ("saldo", "kontostand", "balance")


def _decode(raw: bytes) -> str:
    """Dekodiert die Datei — versucht UTF-8 (mit BOM), fällt auf CP1252 zurück."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    """Erkennt das Trennzeichen — wählt das, das in der ersten echten Zeile am
    häufigsten vorkommt (``;`` ist in CH am üblichsten)."""
    first_line = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first_line.count(d) for d in (";", "\t", ",")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ";"


def _parse_amount(text: str | None) -> Decimal | None:
    """Parst einen Betrag aus CH/DE-Schreibweisen. ``None`` bei leer/ungültig."""
    if not text:
        return None
    s = text.strip().replace("'", "").replace("’", "").replace(" ", "").replace("\xa0", "")
    if not s:
        return None
    if "," in s and "." in s:
        # Beide vorhanden → das hintere Zeichen ist das Dezimaltrennzeichen.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # 1.234,50 → 1234.50
        else:
            s = s.replace(",", "")                      # 1,234.50 → 1234.50
    elif "," in s:
        s = s.replace(",", ".")                         # 1234,50 → 1234.50
    try:
        value = Decimal(s)
        return value.quantize(Decimal("0.01")) if value.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def _parse_date(text: str | None) -> date | None:
    """Parst ``31.05.2026``, ``2026-05-31`` (oder ``31.5.26``)."""
    if not text:
        return None
    s = text.strip()[:10].strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _match_column(
    header: list[str], keywords: tuple[str, ...], belegt: set[int] | None = None
) -> int | None:
    """Index der Header-Spalte, die am besten zu den Schlüsselwörtern passt.

    **Erst genau, dann ungefähr — und nie eine Spalte doppelt.**

    Vorher entschied allein die Reihenfolge von links, und der Vergleich war ein
    Teilstring-Treffer. Damit fing ``"buchung"`` (Schlüsselwort für den
    Buchungstext) das Wort ``"Buchungsdatum"`` — bei der Kopfzeile
    ``Buchungsdatum;Text;Betrag;Saldo`` wurde die DATUMS-Spalte zum Buchungstext,
    und jede Buchung hiess danach nach ihrem Datum. Die Spalte ``Text`` blieb
    ungelesen. Betroffen sind gängige Schweizer Exporte; die Tests kannten nur
    ``Datum;Buchungstext;…``, wo die Reihenfolge zufällig rettete.

    Zwei Änderungen: ein exakter Treffer schlägt jeden Teiltreffer, und Spalten,
    die schon einem anderen Feld gehören, sind tabu.
    """
    belegt = belegt or set()
    for i, cell in enumerate(header):
        if i not in belegt and cell.strip().lower() in keywords:
            return i
    for i, cell in enumerate(header):
        low = cell.strip().lower()
        if i not in belegt and any(kw in low for kw in keywords):
            return i
    return None


def parse_csv_statements(raw: bytes) -> list[Camt053Statement]:
    """Parst eine Bank-CSV und liefert genau **einen** Auszug (als Liste, damit
    der Import-Router CSV und CAMT gleich behandeln kann).

    Wirft ``ValueError``, wenn keine plausiblen Buchungszeilen gefunden werden.
    """
    text = _decode(raw)
    delimiter = _sniff_delimiter(text)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        # csv.Error ist KEIN ValueError — eine Binär-/kaputte Datei (z. B. NUL-Byte)
        # würde sonst als 500 durchschlagen statt als saubere Import-Fehlermeldung.
        raise ValueError(f"CSV nicht lesbar: {exc}") from exc
    rows = [r for r in rows if any(cell.strip() for cell in r)]  # Leerzeilen weg
    if not rows:
        raise ValueError("CSV ist leer.")

    # Header = erste Zeile, die eine Datums- UND eine Betragsspalte erkennt.
    header_idx = None
    for i, row in enumerate(rows[:10]):  # nur die ersten Zeilen nach Header absuchen
        low = [c.strip().lower() for c in row]
        has_date = any(any(k in c for k in _COL_DATE) for c in low)
        has_amount = any(any(k in c for k in (*_COL_AMOUNT, *_COL_DEBIT, *_COL_CREDIT)) for c in low)
        if has_date and has_amount:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("CSV-Spalten nicht erkannt (keine Datum-/Betrag-Spalte gefunden).")

    header = rows[header_idx]
    col_date = _match_column(header, _COL_DATE)
    col_amount = _match_column(header, _COL_AMOUNT)
    col_debit = _match_column(header, _COL_DEBIT)
    col_credit = _match_column(header, _COL_CREDIT)
    col_balance = _match_column(header, _COL_BALANCE)
    # Der Buchungstext zuletzt und nur aus dem, was übrig ist: seine Stichwörter
    # sind die unschärfsten („text", „buchung"), und was Datum, Betrag oder
    # Saldo schon für sich beansprucht haben, kann nicht auch der Text sein.
    col_desc = _match_column(
        header, _COL_DESC,
        belegt={i for i in (col_date, col_amount, col_debit, col_credit, col_balance)
                if i is not None},
    )

    entries: list[Camt053Entry] = []
    last_balance_by_date: tuple[date, Decimal] | None = None
    # **Was der Leser wegwirft, wird gezählt.** Die drei ``continue`` unten sind
    # jedes für sich vernünftig (Summenzeile, Fusszeile, Trennzeile), aber sie
    # verschlucken auch echte Buchungen mit einem Datums- oder Betragsformat, das
    # der Leser nicht kennt. Ohne Zahl daneben sieht der Bericht in beiden Fällen
    # gleich aus.
    weg = 0
    weg_beispiele: list[str] = []

    def _verwerfen(row: list[str]) -> None:
        nonlocal weg
        weg += 1
        if len(weg_beispiele) < 3:
            # Gekürzt und ohne Zellen-Trenner: das Beispiel soll die Zeile
            # wiedererkennbar machen, nicht die Datei nachdrucken.
            weg_beispiele.append(" | ".join(c.strip() for c in row if c.strip())[:120])

    for row in rows[header_idx + 1:]:
        if col_date is None or col_date >= len(row):
            _verwerfen(row)
            continue
        d = _parse_date(row[col_date])
        if d is None:
            _verwerfen(row)
            continue

        # Betrag: entweder eine vorzeichenbehaftete Spalte oder Belastung/Gutschrift.
        amount: Decimal | None = None
        if col_amount is not None and col_amount < len(row):
            amount = _parse_amount(row[col_amount])
        if amount is None and (col_debit is not None or col_credit is not None):
            debit = _parse_amount(row[col_debit]) if (col_debit is not None and col_debit < len(row)) else None
            credit = _parse_amount(row[col_credit]) if (col_credit is not None and col_credit < len(row)) else None
            if credit is not None and credit != 0:
                amount = credit.copy_abs()
            elif debit is not None and debit != 0:
                amount = -debit.copy_abs()
        if amount is None:
            _verwerfen(row)
            continue

        desc = row[col_desc].strip() if (col_desc is not None and col_desc < len(row)) else ""
        entries.append(Camt053Entry(date=d, value_date=None, amount=amount, description=desc, reference=None))

        if col_balance is not None and col_balance < len(row):
            bal = _parse_amount(row[col_balance])
            # ``>`` und nicht ``>=``: bei mehreren Buchungen am selben Tag
            # gewann sonst die zuletzt GELESENE Zeile. Bank-CSVs sind aber
            # üblicherweise absteigend sortiert — die letzte Zeile eines Tages
            # ist dort die zeitlich frühere, also der Saldo VOR den anderen
            # Buchungen dieses Tages. Der Abgleich mit dem Kontoauszug meldete
            # dann eine Abweichung, die es nicht gab.
            if bal is not None and (last_balance_by_date is None or d > last_balance_by_date[0]):
                last_balance_by_date = (d, bal)

    if not entries:
        raise ValueError("Keine gültigen Buchungszeilen in der CSV gefunden.")

    dates = [e.date for e in entries]
    return [
        Camt053Statement(
            iban=None,  # CSV trägt i.d.R. keine IBAN → Zielkonto wird manuell gewählt
            currency=None,
            period_from=min(dates),
            period_to=max(dates),
            opening_balance=None,
            closing_balance=last_balance_by_date[1] if last_balance_by_date else None,
            entries=entries,
            uebersprungene_zeilen=weg,
            uebersprungene_beispiele=weg_beispiele,
        )
    ]
