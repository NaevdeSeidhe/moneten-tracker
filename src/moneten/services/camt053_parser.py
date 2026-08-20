"""Parser für CAMT.053-Kontoauszüge (ISO 20022, Schweizer Standard).

CAMT.053 ist der XML-Standard, den Schweizer Banken im
E-Banking zum Download anbieten. Diese Datei enthält pro Periode alle Buchungen
plus Anfangs- und Schluss-Saldo.

Bewusst **namespace-agnostisch**: Banken liefern verschiedene Versionen
(camt.053.001.02 bis .08) mit unterschiedlichem XML-Namespace. Statt einen
festen Namespace zu erwarten, vergleichen wir nur die *lokalen* Tag-Namen
(der Teil nach ``}``). So funktioniert der Parser über alle Versionen.

Nur Standard-Library (``xml.etree.ElementTree``) — kein externes Paket,
passt zur Offline-First-Vorgabe.

Vorzeichen-Konvention (wie im Rest der App):
* CRDT (Gutschrift / Haben)  → positiver Betrag (Einnahme)
* DBIT (Belastung / Soll)    → negativer Betrag (Ausgabe)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

from moneten.dates import heute_lokal


@dataclass
class Camt053Entry:
    """Eine einzelne Buchung aus dem Auszug."""

    date: date  # Buchungsdatum (BookgDt), Fallback Valuta
    value_date: date | None  # Valuta (ValDt)
    amount: Decimal  # vorzeichenbehaftet: + = Gutschrift, - = Belastung
    description: str  # Buchungstext (RmtInf/Ustrd oder AddtlNtryInf)
    reference: str | None  # AcctSvcrRef (Bank-Referenz)


@dataclass
class Camt053Statement:
    """Ein kompletter Kontoauszug (ein <Stmt>)."""

    iban: str | None
    currency: str | None
    period_from: date | None
    period_to: date | None
    opening_balance: Decimal | None  # OPBD, vorzeichenbehaftet
    closing_balance: Decimal | None  # CLBD, vorzeichenbehaftet
    entries: list[Camt053Entry] = field(default_factory=list)
    # Zeilen, die der Leser nicht verwerten konnte — nur der CSV-Leser füllt das.
    # Beim CAMT gibt es diesen Fall nicht: eine unlesbare Datei fliegt als Ganzes
    # raus. Bei einer CSV verschwinden einzelne Zeilen, und das darf nicht
    # stillschweigend passieren: ein unbekanntes Betragsformat liess 300 Zeilen
    # auf 12 zusammenschrumpfen, und der Bericht meldete zufrieden „12 importiert".
    uebersprungene_zeilen: int = 0
    uebersprungene_beispiele: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level-Helfer: namespace-agnostische Navigation
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    """Lokaler Tag-Name ohne Namespace: ``{urn..}Stmt`` → ``Stmt``."""
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [c for c in elem if _local(c.tag) == name]


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    cs = _children(elem, name)
    return cs[0] if cs else None


def _path(elem: ET.Element | None, *names: str) -> ET.Element | None:
    """Folgt einem Pfad direkter Kinder: ``_path(stmt, "Acct", "Id", "IBAN")``."""
    cur = elem
    for name in names:
        cur = _child(cur, name)
        if cur is None:
            return None
    return cur


def _text(elem: ET.Element | None, *names: str) -> str | None:
    node = _path(elem, *names) if names else elem
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _parse_date(elem: ET.Element | None) -> date | None:
    """Liest ein Datum aus einem ``<Dt>``-Wrapper (CAMT nutzt ``<Dt><Dt>...</Dt></Dt>``).

    Akzeptiert sowohl ``<X><Dt>2026-05-31</Dt></X>`` als auch direkte Datums-Texte
    und ISO-Datetime (nimmt dann den Datumsteil).
    """
    if elem is None:
        return None
    raw = _text(elem, "Dt") or _text(elem, "DtTm") or (elem.text.strip() if elem.text else None)
    if not raw:
        return None
    raw = raw[:10]  # Datetime → Datumsteil
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _signed_amount(node: ET.Element | None) -> Decimal | None:
    """Betrag (``<Amt>``) + Vorzeichen aus dem Geschwister-``<CdtDbtInd>``.

    ``node`` ist das Eltern-Element, das ``Amt`` und ``CdtDbtInd`` enthält
    (z.B. ein ``<Ntry>`` oder ``<Bal>``).
    """
    amt_text = _text(node, "Amt")
    if amt_text is None:
        return None
    try:
        value = Decimal(amt_text)
        if not value.is_finite():  # "NaN"/"Infinity" sind gültige Decimals, hier unerwünscht
            return None
        value = value.quantize(Decimal("0.01"))
    except (ValueError, ArithmeticError):
        return None
    ind = _text(node, "CdtDbtInd")
    if ind == "DBIT":
        value = -value
    return value


# ---------------------------------------------------------------------------
# Haupt-Parser
# ---------------------------------------------------------------------------


def _entry_description(ntry: ET.Element) -> str:
    """Baut den Buchungstext aus den verfügbaren Feldern.

    Priorität: alle ``RmtInf/Ustrd`` (zusammengefügt) → ``AddtlNtryInf`` →
    ``TxDtls/RltdPties``-Namen. Liefert getrimmten String (kann leer sein).
    """
    ustrd_parts: list[str] = []
    # Über alle TxDtls/RmtInf/Ustrd sammeln (kann mehrere geben).
    for ntry_dtls in _children(ntry, "NtryDtls"):
        for tx_dtls in _children(ntry_dtls, "TxDtls"):
            rmt = _child(tx_dtls, "RmtInf")
            for ustrd in _children(rmt, "Ustrd"):
                if ustrd.text and ustrd.text.strip():
                    ustrd_parts.append(ustrd.text.strip())
    if ustrd_parts:
        return " ".join(ustrd_parts)

    addtl = _text(ntry, "AddtlNtryInf")
    if addtl:
        return addtl
    return ""


def _entry_reference(ntry: ET.Element) -> str | None:
    """Bank-Referenz: ``AcctSvcrRef`` auf Ntry- oder TxDtls/Refs-Ebene."""
    ref = _text(ntry, "AcctSvcrRef")
    if ref:
        return ref
    for ntry_dtls in _children(ntry, "NtryDtls"):
        for tx_dtls in _children(ntry_dtls, "TxDtls"):
            ref = _text(tx_dtls, "Refs", "AcctSvcrRef")
            if ref:
                return ref
    return None


def _parse_statement(stmt: ET.Element) -> Camt053Statement:
    """Wandelt ein einzelnes ``<Stmt>``-Element in ein :class:`Camt053Statement`."""
    # Konto
    acct = _child(stmt, "Acct")
    iban = _text(acct, "Id", "IBAN")
    currency = _text(acct, "Ccy")

    # Periode
    frto = _child(stmt, "FrToDt")
    period_from = _parse_date(_child(frto, "FrDtTm")) or _parse_date(_child(frto, "FrDt"))
    period_to = _parse_date(_child(frto, "ToDtTm")) or _parse_date(_child(frto, "ToDt"))

    # Salden: OPBD (opening) und CLBD (closing)
    opening = closing = None
    for bal in _children(stmt, "Bal"):
        code = _text(bal, "Tp", "CdOrPrtry", "Cd")
        amount = _signed_amount(bal)
        if code == "OPBD":
            opening = amount
        elif code == "CLBD":
            closing = amount
        elif code == "PRCD" and opening is None:
            opening = amount  # "Previous closing" als Fallback für Opening

    # Buchungen
    entries: list[Camt053Entry] = []
    for ntry in _children(stmt, "Ntry"):
        amount = _signed_amount(ntry)
        if amount is None:
            continue
        bookg = _parse_date(_child(ntry, "BookgDt"))
        val = _parse_date(_child(ntry, "ValDt"))
        entries.append(
            Camt053Entry(
                date=bookg or val or period_to or heute_lokal(),
                value_date=val,
                amount=amount,
                description=_entry_description(ntry),
                reference=_entry_reference(ntry),
            )
        )

    return Camt053Statement(
        iban=iban,
        currency=currency,
        period_from=period_from,
        period_to=period_to,
        opening_balance=opening,
        closing_balance=closing,
        entries=entries,
    )


class _OhneDtd(ET.TreeBuilder):
    """Weist jede DOCTYPE-Deklaration ab — an welcher Stelle sie auch steht.

    Externe Entities löst expat von sich aus nicht auf (kein XXE, kein Dateileak);
    die INTERNE Expansion ist es, die aus einer kleinen Datei ein Vielfaches an
    Text macht. Expat deckelt die Verstärkung inzwischen selbst, aber erst bei
    etwa Faktor 100 — bei den erlaubten 15 MB Upload sind das immer noch
    Milliarden Zeichen und mehrere Gigabyte, gegen ein Speicherlimit von einem.
    """

    def doctype(self, name, pubid, system):  # noqa: ANN001, ANN201 — Signatur von ET
        raise ValueError("XML mit DTD/Entity-Deklaration wird aus Sicherheitsgründen abgelehnt.")


def parse_camt053_all(xml_bytes: bytes) -> list[Camt053Statement]:
    """Parst eine CAMT.053-Datei und liefert **alle** enthaltenen Auszüge.

    Eine Datei kann mehrere ``<Stmt>`` enthalten — z.B. wenn die Bank alle
    Konten in einem kombinierten Export bündelt. Jeder Auszug trägt seine
    eigene IBAN, sodass der Importer ihn dem richtigen Konto zuordnen kann.

    Wirft ``ValueError`` wenn die Datei kein gültiges CAMT.053 ist.
    """
    # Schutz vor Entity-Expansion-DoS ("billion laughs" / quadratic blowup):
    # ElementTree expandiert interne Entities. Eine CAMT.053-Datei enthält
    # niemals eine DTD/Entity-Deklaration — solche Prologe werden abgelehnt.
    #
    # **Am Parser und nicht am Byte-Fenster.** Vorher wurden die ersten 8 KB
    # nach ``<!doctype`` durchsucht. Nachgemessen: neun Kilobyte Kommentar davor,
    # und die Deklaration steht ausserhalb des Fensters — aus 9'797 Byte Datei
    # wurden 300'000 Zeichen Buchungstext. Der Wächter unten sieht die
    # Deklaration, wo immer sie steht, und kennt keine Fehlalarme: ein
    # Buchungstext, der die Zeichenfolge zufällig enthält, löst ihn nicht aus.
    try:
        root = ET.fromstring(xml_bytes, parser=ET.XMLParser(target=_OhneDtd()))
    except ET.ParseError as exc:
        raise ValueError(f"Datei ist kein gültiges XML: {exc}") from exc

    # <Document><BkToCstmrStmt><Stmt>...
    bk = _child(root, "BkToCstmrStmt")
    if bk is None:
        # Manche Exporte haben <Document> als root weggelassen.
        bk = root if _local(root.tag) == "BkToCstmrStmt" else None
    if bk is None:
        raise ValueError("Keine CAMT.053-Struktur gefunden (BkToCstmrStmt fehlt).")

    stmts = _children(bk, "Stmt")
    if not stmts:
        raise ValueError("Kein Auszug (Stmt) in der Datei gefunden.")
    return [_parse_statement(s) for s in stmts]


def parse_camt053(xml_bytes: bytes) -> Camt053Statement:
    """Parst eine CAMT.053-Datei und liefert den **ersten** Auszug.

    Komfort-Wrapper um :func:`parse_camt053_all` für Single-Konto-Dateien.
    """
    return parse_camt053_all(xml_bytes)[0]


# ---------------------------------------------------------------------------
# Deduplizierung
# ---------------------------------------------------------------------------


def make_dedup_hash(buchungsdatum: date, amount: Decimal, description: str) -> str:
    """Stabiler Fingerabdruck einer Buchung zur Duplikat-Erkennung.

    Hash über Datum + Betrag + die ersten 50 Zeichen des Buchungstexts
    (gemäss Konzept Abschnitt 9). Identische Buchungen aus einem erneuten
    Import desselben Zeitraums erzeugen denselben Hash und werden übersprungen.
    """
    basis = f"{buchungsdatum.isoformat()}|{amount}|{description.strip()[:50]}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
