"""Buchungs-Erfassung und -Liste (Phase 1).

Eine Buchung (``Transaction``) gehört zu einem Konto und (optional) einer
Kategorie. ``amount`` ist vorzeichenbehaftet: positiv = Einnahme, negativ =
Ausgabe. Im Formular wählt der Nutzer „Ausgabe/Einnahme" und gibt den Betrag
positiv ein — das Vorzeichen setzt der Router.

Nach jeder Mutation (anlegen/ändern/löschen) wird der ``current_balance`` des
betroffenen Kontos via ``recalc_account_balance`` neu berechnet. Ändert sich
das Konto beim Bearbeiten, werden altes und neues Konto neu gerechnet.

UI-Muster wie bei den Konten: ein Container ``#transactions-root``, der bei
jeder Aktion komplett neu gerendert wird.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import (
    Account,
    AccountType,
    Attachment,
    Category,
    LohnHerkunft,
    LohnPostenArt,
    ManagementType,
    Transaction,
    TransactionSplit,
    User,
    enthaelt,
    not_transfer,
)
from moneten.db.session import get_db
from moneten.money import parse_amount

# Als Modul und nicht als Einzelfunktionen: die Namen dort sind bewusst kurz
# (``speichern``, ``entfernen``, ``vorschlag``) und wären hier neben den
# Split-Funktionen mehrdeutig. ``lohn.speichern(...)`` sagt, worum es geht.
from moneten.services import lohn, rechnungsbeleg
from moneten.services.attachments import list_receipts, resolve_receipt
from moneten.services.balances import recalc_account_balance
from moneten.services.bulk_assign import (
    BULK_UNDO_MAX,
    bulk_conditions,
    bulk_preview,
    pack_undo,
    unpack_undo,
)
from moneten.services.categorization import learn_from_transaction, suggest_keyword
from moneten.services.receipt_match import attach_receipt, read_receipt_data
from moneten.services.receipt_split import suggest_splits
from moneten.templating import MONATE, templates

router = APIRouter(tags=["transactions"])

# Wie viele Buchungen die Liste maximal zeigt. Grosszügig, damit ein kompletter
# E-Banking-Export sichtbar ist (Monatskarten gruppieren; ältere sind eingeklappt).
LIST_LIMIT = 10000
# Wie viele Monatskarten pro Seite geladen werden. Ältere Monate holt „Ältere
# Monate laden" per HTMX nach — statt (wie früher) bis zu 10'000 Buchungen auf
# einmal zu hydrieren und ins DOM zu rendern (NAS-CPU + Handy-DOM).
MONTH_WINDOW = 6


def _active_accounts(db: Session) -> list[Account]:
    return list(
        db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.sort_order, Account.id))
    )


def _category_groups(db: Session) -> list[tuple[str, list[tuple[int, str]]]]:
    """Liefert Kategorien als optgroup-Struktur: [(Top-Name, [(sub_id, sub_name)..])..]."""
    tops = db.scalars(
        select(Category).where(Category.parent_id.is_(None), Category.is_archived.is_(False)).order_by(Category.sort_order)
    ).all()
    groups: list[tuple[str, list[tuple[int, str]]]] = []
    for top in tops:
        subs = db.scalars(
            select(Category).where(Category.parent_id == top.id, Category.is_archived.is_(False)).order_by(Category.sort_order)
        ).all()
        if subs:
            groups.append((top.name, [(s.id, s.name, s.icon) for s in subs]))
    return groups


def _opt_int(v: str | int | None) -> int | None:
    """Query-/Form-Wert robust zu int|None machen.

    Wichtig für den Filter: die Selects/Picker senden bei „Alle …" einen LEEREN
    String (``account_id=``). Ein FastAPI-Parameter ``int | None`` würde daran mit
    422 scheitern (leerer String ≠ fehlender Parameter) → der Filter-Swap bliebe aus.
    Darum nehmen wir die Filter-IDs als String entgegen und wandeln hier.
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    # ``isdigit()`` ist KEIN Waechter vor ``int()``: hochgestellte Ziffern und
    # eine Handvoll anderer Unicode-Zeichen gelten als Ziffer, lassen sich aber
    # nicht umwandeln. Gemessen: ``"²³".isdigit()`` ist ``True``,
    # ``int("²³")`` wirft ``ValueError`` — aus einem manipulierten
    # Filter wurde so ein Serverfehler statt „Filter ungueltig".
    try:
        return int(s)
    except ValueError:
        return None


# Die Buchungstypen, die :func:`_filter_conditions` kennt. Alles andere wird zu
# ``None`` normalisiert: ein unbekannter Wert (``kind=egal``) sah sonst wie ein
# aktiver Filter aus, erzeugte aber keine einzige Bedingung — und öffnete damit
# die Massen-Zuweisung auf den ganzen Bestand.
KIND_WERTE = ("einnahme", "ausgabe", "transfer")


def _filter_args(
    *, q: str | None, account_id: str | None, category_id: str | None,
    kind: str | None, only_receipts: str | int | None,
) -> dict:
    """Rohe Filter-Formularfelder → normalisierte kwargs.

    Dieselben fünf Felder reisen mit jeder Aktion mit (``hx-include="#tx-filter"``)
    und werden an drei Stellen gebraucht: Bedingungen bauen, Liste neu rendern,
    Rückgängig-Aufruf. Einmal normalisieren statt dreimal — bei der
    Massen-Zuweisung entscheidet genau diese Deckungsgleichheit darüber, ob die
    Vorschau von denselben Buchungen spricht wie der Knopf.

    ``q`` wird hier **getrimmt**: ein Suchtext aus lauter Leerzeichen zählte
    sonst als aktiver Filter, während :func:`_filter_conditions` daraus
    ``ilike("%%")`` macht — also jede Buchung. Genau diese Kombination hebelt bei
    der Massen-Zuweisung die Schutzregel „nur mit aktivem Filter" aus.

    ``kind`` wird auf die bekannten Werte eingeschränkt (siehe
    :data:`KIND_WERTE`) — aus demselben Grund: was keine Bedingung erzeugt, darf
    auch nirgends als Filter durchgehen.
    """
    return {
        "q": (q or "").strip() or None,
        "account_id": _opt_int(account_id),
        "category_id": _opt_int(category_id),
        "kind": kind if kind in KIND_WERTE else None,
        "only_receipts": bool(_opt_int(only_receipts)),
    }


def _filter_conditions(
    *, q: str | None, account_id: int | None,
    category_id: int | None, kind: str | None, only_receipts: bool = False,
) -> list:
    """Gemeinsame WHERE-Bedingungen des Buchungsfilters (Monats-Fenster + Gesamtsumme).

    Die Rückgabe ist gleichzeitig die Antwort auf „ist überhaupt gefiltert?" —
    eine leere Liste heisst: der ganze Bestand. Deshalb hängen sowohl die Leiste
    der Massen-Zuweisung als auch deren Schutzabfrage an DIESER Liste und nicht
    an den rohen Formularwerten. Ein Wert, der keine Bedingung erzeugt, ist kein
    Filter, auch wenn er im Formular steht.
    """
    conds: list = []
    if account_id:
        conds.append(Transaction.account_id == account_id)
    if category_id:
        conds.append(Transaction.category_id == category_id)
    if only_receipts:
        # Nur Buchungen, zu denen ein Beleg/Anhang hinterlegt ist.
        conds.append(Transaction.id.in_(select(Attachment.transaction_id)))
    # Bei Einnahme/Ausgabe Transfers (Umbuchungen) ausschliessen — sie sind weder.
    if kind == "einnahme":
        conds.extend((Transaction.amount >= 0, not_transfer()))
    elif kind == "ausgabe":
        conds.extend((Transaction.amount < 0, not_transfer()))
    elif kind == "transfer":
        conds.append(Transaction.management_type == ManagementType.TRANSFER)
    if q and q.strip():
        # enthaelt() maskiert „%" und „_": ohne das trifft die Suche „%" den
        # ganzen Bestand, und die Massen-Zuweisung hält das für einen Filter.
        conds.append(enthaelt(Transaction.description, q.strip()))
    return conds


# ---------------------------------------------------------------------------
# Zeitraum — EIN Umschalter für Summenzeile UND Liste, bewusst NEBEN dem Filter
# ---------------------------------------------------------------------------
#
# Frueher gab es hier zwei Umschalter: ``sum_period`` (Monat/Jahr/Gesamt)
# über der Summenzeile und ``zeitraum`` (Jahr/Alle Jahre) über den Monatskarten.
# Beide trugen einen Knopf mit der blossen Jahreszahl, standen drei Zeilen
# auseinander und konnten verschiedene Zeiträume behaupten — die Summe sprach
# über alle Jahre, die Liste darunter zeigte nur das laufende. Zwei Zahlen
# übereinander, die sich widersprechen, sind schlimmer als eine falsche Vorgabe.
# Seither begrenzt EIN Wert beides, und die Beschriftung der Leitzahl („Saldo
# August 2026") sagt, welcher Zeitraum gerade gilt.

# Vorgabewert beim Aufruf ohne Parameter: das laufende Kalenderjahr.
ZEITRAUM_VORGABE = "jahr"

# Erlaubte Werte, in der Reihenfolge der Leiste (eng → weit).
ZEITRAEUME = ("monat", "jahr", "alles")


def _zeitraum_wert(roh: str | None) -> str:
    """Rohwert → ``"monat"``, ``"jahr"`` (Vorgabe) oder ``"alles"``.

    Alles Unbekannte wird zur Vorgabe — genau andersherum als bei ``kind``, wo
    ein unbekannter Wert zu „kein Filter" wird. Der Grund ist derselbe: der
    Tippfehler soll in die harmlose Richtung fallen. Harmlos ist hier die
    ENGERE Menge, dort die weitere.
    """
    wert = (roh or "").strip()
    return wert if wert in ZEITRAEUME else ZEITRAUM_VORGABE


def _jahr_grenzen(today: date) -> tuple[date, date]:
    """``[Anfang, Ende)`` des laufenden Kalenderjahres."""
    return date(today.year, 1, 1), date(today.year + 1, 1, 1)


def _monat_grenzen(today: date) -> tuple[date, date]:
    """``[Anfang, Ende)`` des laufenden Monats."""
    start = today.replace(day=1)
    ende = (date(start.year + 1, 1, 1) if start.month == 12
            else date(start.year, start.month + 1, 1))
    return start, ende


def _zeitraum_grenzen(zeitraum: str, today: date) -> tuple[date, date] | None:
    """``[Anfang, Ende)`` des gewählten Zeitraums — ``None`` bei „alle Jahre".

    Eine Quelle für Bedingung, Ausserhalb-Zählung und die Rückfall-Regel beim
    Speichern. Lägen die Grenzen an drei Stellen, könnte eine Buchung in der
    Liste fehlen, die die Zählung daneben als „drin" führt.
    """
    if zeitraum == "monat":
        return _monat_grenzen(today)
    if zeitraum == "jahr":
        return _jahr_grenzen(today)
    return None


def _zeitraum_bedingungen(zeitraum: str, today: date) -> list:
    """WHERE-Zusatz für den Zeitraum — NICHT Teil von :func:`_filter_conditions`.

    Getrennt gehalten, und zwar aus zwei Gründen:

    * An der Filterliste hängt die Schutzregel der Massen-Zuweisung („leer =
      ganzer Bestand"). Zählte das Jahr als Filter, erschiene die
      Zuweisungs-Leiste schon beim blossen Öffnen der Seite und böte den
      kompletten Jahrgang auf einen Klick an.
    * Die Vorgabe ist ein ANZEIGE-Wert. Die Routen, die etwas verändern, dürfen
      sie nicht geschenkt bekommen: dort gilt der Zeitraum nur, wenn er
      ausdrücklich mitgeschickt wird (siehe :func:`assign_filtered`).
    """
    grenzen = _zeitraum_grenzen(zeitraum, today)
    if grenzen is None:
        return []
    start, ende = grenzen
    # Beidseitig begrenzt: eine bloss untere Grenze liesse vordatierte Buchungen
    # (Dauerauftrag fürs neue Jahr) unter der Beschriftung „2026" mitlaufen.
    return [Transaction.date >= start, Transaction.date < ende]


def _zeitraum_labels(today: date) -> dict[str, str]:
    """Beschriftung der drei Knöpfe — der aktive nennt den geltenden Zeitraum."""
    return {"monat": MONATE[today.month - 1], "jahr": str(today.year), "alles": "Alle Jahre"}


def _zeitraum_titel(zeitraum: str, today: date) -> str:
    """Beschriftung der Leitzahl. Ein nacktes „Netto" war genau der Punkt, an dem
    die Zahl unverständlich wurde („Einnahmen minus Ausgaben, netto 6k?") — sie
    muss sagen, über WELCHEN Zeitraum sie spricht."""
    if zeitraum == "monat":
        return f"Saldo {MONATE[today.month - 1]} {today.year}"
    if zeitraum == "jahr":
        return f"Saldo {today.year}"
    return "Saldo über alle Buchungen"


def _zeitraum_kurz(zeitraum: str, today: date) -> str:
    """Der Zeitraum als blosse Zeitangabe („August 2026") — leer bei „alle Jahre".

    Für Stellen, die den Zeitraum in Klammern oder hinter „aus" setzen
    (Massen-Zuweisung). Leer heisst dort: die Aktion ist nicht begrenzt.
    """
    if zeitraum == "monat":
        return f"{MONATE[today.month - 1]} {today.year}"
    if zeitraum == "jahr":
        return str(today.year)
    return ""


def _zeitraum_satz(zeitraum: str, today: date) -> str:
    """Der Zeitraum als Satzteil („im August 2026") für die leeren Zustände —
    leer bei „alle Jahre", wo eine Zeitangabe nichts erklären würde."""
    if zeitraum == "monat":
        return f"im {MONATE[today.month - 1]} {today.year}"
    if zeitraum == "jahr":
        return f"in {today.year}"
    return ""


def _ausserhalb_zaehlen(db: Session, conds: list, today: date, zeitraum: str) -> int:
    """Treffer des Filters, die ausserhalb des gewählten Zeitraums liegen.

    Ohne diese Zahl sieht die Jahres-Vorgabe wie fehlende Daten aus: wer einen
    Beleg von 2024 sucht, bekommt „keine Buchungen" und hält den Import für
    verloren. Die Zahl steht deshalb neben dem Umschalter und im leeren Zustand.
    """
    grenzen = _zeitraum_grenzen(zeitraum, today)
    if grenzen is None:
        return 0
    start, ende = grenzen
    return db.scalar(
        select(func.count()).select_from(Transaction)
        .where(*conds, or_(Transaction.date < start, Transaction.date >= ende))
    ) or 0


def _filtered_transactions(
    db: Session, *, q: str | None, account_id: int | None,
    category_id: int | None, kind: str | None, only_receipts: bool = False,
    before: date | None = None, extend_to: date | None = None,
    zeit_conds: list | None = None,
) -> tuple[list[Transaction], str | None]:
    """Top-Level-Buchungen, gefiltert, als **Monats-Fenster**: die neuesten
    ``MONTH_WINDOW`` Monate (vor ``before``), immer ganze Monate (Subtotale!).

    ``zeit_conds`` (aus :func:`_zeitraum_bedingungen`) begrenzt zusätzlich das
    Jahr. Es geht in dieselbe Bedingungsliste wie der Filter — sonst zählte die
    Monatssuche Monate mit, die die Liste hinterher gar nicht zeigt, und
    „Ältere Monate laden" böte leere Karten an.

    Rückgabe ``(txs, next_before)`` — ``next_before`` ist der Monatsanfang des
    ältesten geladenen Monats (ISO), wenn dahinter noch ältere Monate liegen;
    „Ältere Monate laden" holt sie per HTMX nach. Vorher wurden bis zu 10'000
    Buchungen auf einmal hydriert und gerendert."""
    conds = _filter_conditions(q=q, account_id=account_id, category_id=category_id,
                               kind=kind, only_receipts=only_receipts)
    if zeit_conds:
        conds = [*conds, *zeit_conds]
    if before is not None:
        conds = [*conds, Transaction.date < before]
    ym = func.strftime("%Y-%m", Transaction.date)
    months = list(db.scalars(
        select(ym).where(*conds).group_by(ym).order_by(ym.desc()).limit(MONTH_WINDOW + 1)
    ))
    if not months:
        return [], None
    window = months[:MONTH_WINDOW]
    y, m = (int(p) for p in window[-1].split("-"))
    window_start = date(y, m, 1)
    has_more = len(months) > MONTH_WINDOW
    if extend_to is not None and extend_to < window_start:
        # Fenster bis zum Monat einer bestimmten Buchung ausdehnen — z. B. wenn aus
        # einer NACHGELADENEN, älteren Monatskarte heraus eine Kategorie zugewiesen
        # wird: die Antwort rendert das Fenster neu, und ohne Ausdehnung wäre genau
        # die eben bearbeitete Zeile nicht mehr enthalten (sie „verschwindet").
        window_start = extend_to
        has_more = db.scalar(
            select(Transaction.id).where(*conds, Transaction.date < window_start).limit(1)
        ) is not None
    next_before = window_start.isoformat() if has_more else None
    stmt = (
        select(Transaction).where(*conds, Transaction.date >= window_start)
        .order_by(Transaction.date.desc(), Transaction.id.desc()).limit(LIST_LIMIT)
    )
    return list(db.scalars(stmt)), next_before


def _group_by_month(txs: list[Transaction]) -> list[dict]:
    """Gruppiert (bereits absteigend sortierte) Buchungen nach Monat mit Subtotalen.

    Transfers zählen nicht in Einnahme/Ausgabe (sind weder noch).
    """
    groups: list[dict] = []
    current: dict | None = None
    for tx in txs:
        key = (tx.date.year, tx.date.month)
        if current is None or current["key"] != key:
            current = {
                "key": key,
                "label": f"{MONATE[tx.date.month - 1]} {tx.date.year}",
                "month_label": MONATE[tx.date.month - 1],
                "year": tx.date.year,
                "txs": [], "income": Decimal("0"), "expense": Decimal("0"),
            }
            groups.append(current)
        current["txs"].append(tx)
        if tx.management_type != ManagementType.TRANSFER:
            if tx.amount >= 0:
                current["income"] += tx.amount
            else:
                current["expense"] += -tx.amount
    for g in groups:
        g["net"] = g["income"] - g["expense"]
        g["count"] = len(g["txs"])
    return groups


def _summary_filtered(db: Session, conds: list) -> dict:
    """Gesamtsummen über den **ganzen** Filter (nicht nur die geladenen Monate) —
    eine aggregierte Query statt tausender hydrierter ORM-Objekte. Transfers zählen
    nicht in Einnahme/Ausgabe (wie in :func:`_group_by_month`)."""
    inc = func.coalesce(func.sum(case(
        (and_(Transaction.amount >= 0, not_transfer()), Transaction.amount), else_=0)), 0)
    exp = func.coalesce(func.sum(case(
        (and_(Transaction.amount < 0, not_transfer()), -Transaction.amount), else_=0)), 0)
    row = db.execute(select(inc, exp, func.count()).where(*conds)).one()
    income = Decimal(str(row[0])).quantize(Decimal("0.01"))
    expense = Decimal(str(row[1])).quantize(Decimal("0.01"))
    return {"income": income, "expense": expense, "net": income - expense, "count": row[2]}


# SQLite-Parameterlimit: IN-Listen über ~900 IDs vermeiden (dann alle laden).
_IN_LIMIT = 900


def _attachments_by_tx(db: Session, tx_ids: list[int]) -> dict[int, list[dict]]:
    """Pro (sichtbarer) Buchung die verknüpften Quittungs-Infos (Icon + Detail-Popup):
    Dateiname, erkannter Betrag/Methode. Der OCR-Text wird NICHT mehr mitgegeben —
    das Popup holt ihn on demand (``/attachment/{id}/ocr-text``), statt ~1.5 KB pro
    Beleg in jede Listen-Antwort zu packen.

    ``posten`` trägt die Aufstellung eines geprüften Rechnungsbelegs (siehe
    :func:`~moneten.services.rechnungsbeleg.anzeige_posten`); für jeden anderen
    Beleg ist sie leer, und die Zeile bleibt so hoch, wie sie war."""
    if not tx_ids:
        return {}
    stmt = select(Attachment)
    if len(tx_ids) <= _IN_LIMIT:
        stmt = stmt.where(Attachment.transaction_id.in_(tx_ids))
    out: dict[int, list[dict]] = {}
    for att in db.scalars(stmt):
        if att.transaction_id is None:
            continue
        meta = {}
        if att.parsed_items_json:
            try:
                meta = json.loads(att.parsed_items_json)
            except (ValueError, TypeError):
                meta = {}
        out.setdefault(att.transaction_id, []).append({
            "id": att.id,
            "name": att.original_name,
            "amount": meta.get("amount"),
            "method": meta.get("method"),
            "posten": rechnungsbeleg.anzeige_posten(meta),
        })
    return out


def _splits_by_tx(db: Session, tx_ids: list[int]) -> dict[int, list[dict]]:
    """Pro aufgeteilter (sichtbarer) Buchung die Anteile (Kategorie-Name + Betrag)
    für die Listen-Anzeige (Pille „Aufgeteilt · N") und das Beleg-Popup."""
    if not tx_ids:
        return {}
    cat_names = {c.id: c.name for c in db.scalars(select(Category))}
    stmt = select(TransactionSplit)
    if len(tx_ids) <= _IN_LIMIT:
        stmt = stmt.where(TransactionSplit.transaction_id.in_(tx_ids))
    out: dict[int, list[dict]] = {}
    for sp in db.scalars(stmt):
        out.setdefault(sp.transaction_id, []).append({
            "name": cat_names.get(sp.category_id) or "Ohne Kategorie",
            "amount": f"{sp.amount.copy_abs():.2f}",
        })
    return out


def _split_rows_for(tx: Transaction) -> list[dict]:
    """Editor-Zeilen für die Aufteilung: vorhandene Splits, sonst zwei leere
    Zeilen als Einladung zum Aufteilen."""
    if tx.is_split and tx.splits:
        return [
            {"category_id": sp.category_id or "", "amount": f"{sp.amount.copy_abs():.2f}"}
            for sp in tx.splits
        ]
    return [{"category_id": "", "amount": ""}, {"category_id": "", "amount": ""}]


def _lohn_editor_rows(db: Session, tx: Transaction) -> tuple[list[dict], str | None]:
    """Editor-Zeilen der Lohnzusammensetzung: das Gespeicherte, sonst ein Vorschlag.

    Die Zeilen tragen ihre bisherige Herkunft mit (``herkunft``). Beim Speichern
    entscheidet der Vergleich mit dem unveränderten Betrag darüber, ob ein
    Posten als erfasst oder als gerechnet gilt — siehe
    :func:`moneten.services.lohn.herkunft_nach_aenderung`.
    """
    abrechnung = lohn.abrechnung_zu(db, tx.id)
    if abrechnung is not None and abrechnung.posten:
        return [
            {"label": p.label, "art": p.art.value,
             "betrag": f"{p.betrag:.2f}", "herkunft": p.herkunft.value}
            for p in abrechnung.posten
        ], abrechnung.grundlage
    return lohn.vorschlag(db, tx)


def _parse_lohn_rows(
    arten: list[str], labels: list[str], betraege: list[str],
    alt_betraege: list[str], alt_herkuenfte: list[str],
) -> tuple[list[tuple[LohnPostenArt, str, Decimal, LohnHerkunft]], str | None]:
    """Validiert die Editor-Zeilen → Posten. Zweiter Rückgabewert ist ein Fehlertext.

    Leere Zeilen fallen weg: der Vorschlag stellt NBUV, KTG und Pensionskasse
    leer hin, wenn es dafür keine Quelle gibt. Wer sie nicht ausfüllt, soll nicht
    lauter Nullposten gespeichert bekommen — eine Null behauptet, es gebe diesen
    Abzug nicht, während „leer" heisst, dass er unbekannt ist.
    """
    rows: list[tuple[LohnPostenArt, str, Decimal, LohnHerkunft]] = []
    for art_roh, label_roh, betrag_roh, alt_roh, herkunft_roh in zip(
        arten, labels, betraege, alt_betraege, alt_herkuenfte, strict=False
    ):
        label = (label_roh or "").strip()
        roh = (betrag_roh or "").strip()
        if not roh and not label:
            continue
        try:
            betrag = parse_amount(roh)
        except InvalidOperation:
            return [], "Ein Betrag der Lohnzusammensetzung ist keine gültige Zahl."
        if betrag == 0:
            continue
        if betrag < 0:
            return [], "Beträge der Lohnzusammensetzung sind positiv — die Art bestimmt das Vorzeichen."
        if not label:
            return [], "Jede Position der Lohnzusammensetzung braucht eine Bezeichnung."
        art = LohnPostenArt.BRUTTO if art_roh == LohnPostenArt.BRUTTO.value else LohnPostenArt.ABZUG
        try:
            alt = parse_amount(alt_roh) if (alt_roh or "").strip() else None
        except InvalidOperation:
            alt = None
        rows.append((art, label[:60], betrag,
                     lohn.herkunft_nach_aenderung(betrag, alt, (herkunft_roh or "").strip())))
    return rows, None


# Wortlaut an EINER Stelle: die Gegenprobe im Editor nennt denselben Grund, aus
# dem das Speichern gleich ablehnen wird.
LOHN_OHNE_BRUTTO = "Ohne Bruttolohn ergibt sich kein Nettolohn — bitte den Bruttobetrag eintragen."


def _lohn_rows_geprueft(
    arten: list[str], labels: list[str], betraege: list[str],
    alt_betraege: list[str], alt_herkuenfte: list[str],
) -> tuple[list[tuple[LohnPostenArt, str, Decimal, LohnHerkunft]], str | None]:
    """Die Zeilen so, wie das Speichern sie annimmt — oder der Grund der Ablehnung.

    Eine Quelle für die Route und für die mitlaufende Gegenprobe. Vorher prüfte
    nur die Route: die Vorschau rechnete mit negativen Beträgen weiter und zeigte
    einen Nettolohn samt Differenz, den es nach dem Speichern nie geben konnte.
    Eine leere Liste ohne Fehlertext heisst „nichts zu speichern" — das ist keine
    Ablehnung, sondern das Entfernen der Aufstellung.
    """
    rows, err = _parse_lohn_rows(arten, labels, betraege, alt_betraege, alt_herkuenfte)
    if err:
        return [], err
    if rows and not any(art == LohnPostenArt.BRUTTO for art, _label, _betrag, _h in rows):
        return [], LOHN_OHNE_BRUTTO
    return rows, None


def _lohn_marken(
    keys: list[str], betraege: list[str],
    alt_betraege: list[str], alt_herkuenfte: list[str],
) -> list[tuple[str, str, str]]:
    """Je Editor-Zeile: Schlüssel, Zeichen und Erklärung nach dem Speichern.

    Dieselbe Entscheidung wie beim Speichern (``herkunft_nach_aenderung``) und
    dieselbe Zeichentabelle wie die Anzeige (``lohn.MARKE``), damit die Marke im
    Eingabefeld und das Zeichen nach dem Speichern dasselbe bedeuten. Der
    Schlüssel kommt aus dem Formular und landet in einer HTML-Id — er wird darum
    auf Buchstaben und Ziffern eingegrenzt.
    """
    marken: list[tuple[str, str, str]] = []
    for key, betrag_roh, alt_roh, herkunft_roh in zip(
        keys, betraege, alt_betraege, alt_herkuenfte, strict=False
    ):
        key = (key or "").strip()
        if not key.isalnum() or len(key) > 24:
            continue
        try:
            betrag = parse_amount(betrag_roh or "")
            alt = parse_amount(alt_roh) if (alt_roh or "").strip() else None
        except InvalidOperation:
            marken.append((key, "", ""))
            continue
        # Eine leere oder auf null gesetzte Zeile wird gar nicht gespeichert —
        # sie darf sich auch nicht als Ableitung ausgeben.
        if betrag == 0:
            marken.append((key, "", ""))
            continue
        herkunft = lohn.herkunft_nach_aenderung(betrag, alt, (herkunft_roh or "").strip())
        marken.append((key, lohn.MARKE[herkunft], lohn.LEGENDE.get(herkunft, "")))
    return marken


def _bulk_context(
    db: Session, fa: dict, *, overwrite: bool, assign_category_id: str,
    zeitraum: str = "alles", today: date | None = None,
) -> dict:
    """Kontext der Massen-Zuweisungs-Leiste (``partials/tx_bulk.html``).

    ``ziel_*`` ist das, was der Knopf wirklich anfassen würde — die Zahl in der
    Sicherheitsabfrage und die Zahl im Ergebnis kommen aus derselben Bedingung
    und können darum nicht auseinander laufen.

    ``zeitraum`` gehört zu genau dieser Deckungsgleichheit: die Liste zeigt
    standardmässig nur das laufende Jahr, also muss die Leiste über dieselbe
    Menge sprechen. Ohne das stünde über einer Liste mit 60 Zeilen ein Knopf,
    der 195 Buchungen anfasst — die teuerste Sorte Missverständnis. Die
    Beschriftung nennt den Zeitraum deshalb ausdrücklich (``zeit_label``).

    ``hx_vals`` friert **denselben** Filter ein, aus dem diese Zahlen gerechnet
    wurden, und schickt ihn beim Klick mit. Vorher las der Knopf die Filterfelder
    per ``hx-include`` im Moment des Klicks, während der Bestätigungstext von der
    letzten Antwort stammte: wer nach dem Rendern (z.B. während der 400-ms-
    Verzögerung des Suchfelds) noch etwas tippte, bestätigte eine Zahl und löste
    eine andere Menge aus. Eingefroren ist beides garantiert dieselbe Menge.
    """
    heute = today or heute_lokal()
    zeit_conds = _zeitraum_bedingungen(zeitraum, heute)
    conds = [*_filter_conditions(**fa), *zeit_conds]
    bulk = bulk_preview(db, conds)
    # Nur nennen, wenn er auch einschränkt — „(alle Jahre)" wäre Lärm.
    bulk["zeit_label"] = _zeitraum_kurz(zeitraum, heute)
    bulk["overwrite"] = overwrite
    bulk["ziel_count"] = bulk["alle_count"] if overwrite else bulk["offen_count"]
    bulk["ziel_sum"] = bulk["alle_sum"] if overwrite else bulk["offen_sum"]
    bulk["undo_moeglich"] = bulk["ziel_count"] <= BULK_UNDO_MAX
    bulk["max_undo"] = BULK_UNDO_MAX
    bulk["assign_category_id"] = assign_category_id
    bulk["hx_vals"] = json.dumps({
        "q": fa["q"] or "",
        "account_id": str(fa["account_id"] or ""),
        "category_id": str(fa["category_id"] or ""),
        "kind": fa["kind"] or "",
        "only_receipts": "1" if fa["only_receipts"] else "0",
        # Der Zeitraum reist eingefroren mit — und nur so kommt er überhaupt zur
        # Zuweisung: /assign-filtered kennt von sich aus keinen Jahres-Vorgabewert.
        "zeitraum": zeitraum,
        "overwrite": "1" if overwrite else "0",
    })
    return bulk


def _base_context(
    db: Session,
    *,
    form_mode: str,
    edit_tx: Transaction | None,
    error: str | None,
    info: str | None = None,
    q: str | None,
    account_id: int | None,
    category_id: int | None,
    kind: str | None,
    only_receipts: bool = False,
    split_rows: list[dict] | None = None,
    split_note: str | None = None,
    lohn_rows: list[dict] | None = None,
    lohn_grundlage: str | None = None,
    keep_visible: int | None = None,
    zeitraum: str = ZEITRAUM_VORGABE,
    form_values: dict | None = None,
) -> dict:
    """Baut den gemeinsamen Kontext für Voll-Seite und HTMX-Partial.

    ``form_values`` sind die zuletzt abgeschickten ROHWERTE. Nach einem
    Validierungsfehler rendert das Template das Formular damit vorbefüllt —
    vorher stand man vor einer leeren Maske und musste alles neu tippen.

    ``zeitraum`` begrenzt Summenzeile UND Liste (Vorgabe: laufendes Jahr). Eine
    Grenze für beides, weil die Summe über der Liste sonst über eine andere
    Menge spricht als die Liste selbst — und genau das war der Fehler: „Saldo
    über alle Buchungen" stand über einer Liste, die nur 2026 zeigte.
    """
    # Filterwerte EINMAL normalisieren — dieselbe Funktion wie bei allen
    # schreibenden Routen. Sonst gilt „   " (nur Leerzeichen) oder ein
    # unbekanntes ``kind`` hier als aktiver Filter, erzeugt aber unten keine
    # Bedingung: die Leiste böte einen Knopf an, den der Server sicher ablehnt.
    fa = _filter_args(q=q, account_id=str(account_id or ""),
                      category_id=str(category_id or ""), kind=kind,
                      only_receipts=1 if only_receipts else 0)
    q, account_id = fa["q"], fa["account_id"]
    category_id, kind = fa["category_id"], fa["kind"]
    only_receipts = fa["only_receipts"]
    today = heute_lokal()
    zeitraum = _zeitraum_wert(zeitraum)
    zeit_conds = _zeitraum_bedingungen(zeitraum, today)

    edit_attachments: list[Attachment] = []
    available_receipts: list = []
    learn_keyword = ""
    has_receipt_ocr = False
    lohn_moeglich = False
    lohn_erfasst = False
    if edit_tx is not None:
        edit_attachments = list(
            db.scalars(select(Attachment).where(Attachment.transaction_id == edit_tx.id))
        )
        available_receipts = list_receipts()
        learn_keyword = suggest_keyword(edit_tx.description)
        has_receipt_ocr = any((att.ocr_text or "").strip() for att in edit_attachments)
        if split_rows is None:
            split_rows = _split_rows_for(edit_tx)
        # Nettolohn ist eine Einnahme — nur dort ergibt eine Aufschlüsselung Sinn.
        lohn_moeglich = lohn.darf_aufschluesseln(edit_tx)
        if lohn_moeglich:
            lohn_erfasst = lohn.abrechnung_zu(db, edit_tx.id) is not None
            if lohn_rows is None:
                lohn_rows, lohn_grundlage = _lohn_editor_rows(db, edit_tx)
    # Fenster bis zur gerade bearbeiteten Buchung ausdehnen: nach einer Zuweisung
    # aus der Liste soll man das Ergebnis sehen. Ohne das fiele eine Buchung aus
    # einem nachgeladenen, älteren Monat aus dem Fenster — der Nutzer klickt und
    # die Zeile ist einfach weg.
    extend_to = None
    if keep_visible:
        ref_tx = db.get(Transaction, keep_visible)
        if ref_tx is not None:
            extend_to = ref_tx.date.replace(day=1)
            grenzen = _zeitraum_grenzen(zeitraum, today)
            if grenzen and not (grenzen[0] <= ref_tx.date < grenzen[1]):
                # Dieselbe Regel wie beim Monatsfenster, eine Ebene höher: liegt
                # die eben angefasste Buchung ausserhalb des Zeitraums (Nachtrag
                # für Dezember, vordatierter Dauerauftrag), gilt für DIESE Antwort
                # „alle Jahre". Sonst speichert der Nutzer und die Zeile ist weg —
                # ununterscheidbar davon, dass gar nicht gespeichert wurde.
                zeitraum, zeit_conds = "alles", []
    txs, next_before = _filtered_transactions(db, q=q, account_id=account_id,
                                              category_id=category_id, kind=kind,
                                              only_receipts=only_receipts,
                                              extend_to=extend_to, zeit_conds=zeit_conds)
    groups = _group_by_month(txs)
    tx_ids = [t.id for t in txs]
    attachments_by_tx = _attachments_by_tx(db, tx_ids)
    splits_by_tx = _splits_by_tx(db, tx_ids)
    conds = _filter_conditions(q=q, account_id=account_id, category_id=category_id,
                               kind=kind, only_receipts=only_receipts)
    ausserhalb = _ausserhalb_zaehlen(db, conds, today, zeitraum)
    accounts = _active_accounts(db)
    # „Aktiv" heisst: es gibt mindestens eine WHERE-Bedingung. Am rohen
    # Formularwert gemessen (so war es) galt auch etwas als Filter, das gar
    # nichts einschränkt — und die Leiste erschien über dem ganzen Bestand.
    filter_active = bool(conds)
    lohn_by_tx = lohn.aufstellungen_zu(db, txs)
    return {
        "month_groups": groups,
        "next_before": next_before,
        "primary_acc_id": accounts[0].id if accounts else None,
        # Summe und Liste über DIESELBEN Bedingungen — der Zeitraum steckt schon
        # in ``zeit_conds``, den auch ``_filtered_transactions`` bekommen hat.
        "tx_summary": _summary_filtered(db, [*conds, *zeit_conds]),
        "accounts": accounts,
        "form_values": form_values or {},
        "category_groups": _category_groups(db),
        "form_mode": form_mode,
        "edit_tx": edit_tx,
        "edit_attachments": edit_attachments,
        "available_receipts": available_receipts,
        "attachments_by_tx": attachments_by_tx,
        "attachment_tx_ids": set(attachments_by_tx.keys()),
        "splits_by_tx": splits_by_tx,
        # Aufstellungen der sichtbaren Buchungen: die Zeile klappt sie auf.
        "lohn_by_tx": lohn_by_tx,
        # Die Jahresprobe je Jahr, nicht je Buchung: sie ist eine Aussage über
        # das ganze Jahr, und zwölf Zeilen fragen dieselbe Frage. Berechnet wird
        # sie nur für Jahre, in denen wirklich eine Aufstellung sichtbar ist —
        # sonst kostete jede Buchungsliste eine Abfrage für nichts.
        "lohn_proben": lohn.jahresproben(
            db, {t.date.year for t in txs if t.id in lohn_by_tx}
        ),
        "split_rows": split_rows or [],
        "split_note": split_note,
        "lohn_rows": lohn_rows or [],
        "lohn_grundlage": lohn_grundlage,
        "lohn_moeglich": lohn_moeglich,
        "lohn_erfasst": lohn_erfasst,
        "has_receipt_ocr": has_receipt_ocr,
        "today": heute_lokal().isoformat(),
        "error": error,
        "info": info,
        "learn_keyword": learn_keyword,
        "filter": {
            "q": q or "", "account_id": account_id or "",
            "category_id": category_id or "", "kind": kind or "",
            "only_receipts": only_receipts, "zeitraum": zeitraum,
        },
        "filter_active": filter_active,
        "zeitraum": zeitraum,
        "zeitraum_labels": _zeitraum_labels(today),
        "zeitraum_titel": _zeitraum_titel(zeitraum, today),
        "zeitraum_satz": _zeitraum_satz(zeitraum, today),
        "ausserhalb": ausserhalb,
        # Massen-Zuweisung nur bei aktivem Filter: ohne Filter wären „alle Treffer"
        # der ganze Bestand — das ist keine Zuweisung mehr, das ist ein Unfall.
        # Der Zeitraum zählt dabei ausdrücklich NICHT als Filter (sonst stünde die
        # Leiste beim blossen Öffnen der Seite da), begrenzt die Menge darin aber
        # sehr wohl — die Leiste spricht über das, was die Liste zeigt.
        # Ohne Filter also gar nicht erst rechnen (zwei Aggregate gespart).
        "bulk": (_bulk_context(db, fa, overwrite=False, assign_category_id="",
                               zeitraum=zeitraum, today=today)
                 if filter_active else None),
    }


def _render_root(
    request: Request,
    db: Session,
    *,
    form_mode: str = "none",
    edit_tx: Transaction | None = None,
    error: str | None = None,
    info: str | None = None,
    status_code: int = 200,
    q: str | None = None,
    account_id: int | None = None,
    category_id: int | None = None,
    kind: str | None = None,
    only_receipts: bool = False,
    split_rows: list[dict] | None = None,
    split_note: str | None = None,
    lohn_rows: list[dict] | None = None,
    lohn_grundlage: str | None = None,
    keep_visible: int | None = None,
    zeitraum: str = ZEITRAUM_VORGABE,
    form_values: dict | None = None,
) -> Response:
    """Rendert den ``#transactions-root``-Container."""
    ctx = _base_context(db, form_mode=form_mode, edit_tx=edit_tx, error=error, info=info,
                        q=q, account_id=account_id, category_id=category_id, kind=kind,
                        only_receipts=only_receipts, split_rows=split_rows, split_note=split_note,
                        lohn_rows=lohn_rows, lohn_grundlage=lohn_grundlage,
                        keep_visible=keep_visible, zeitraum=zeitraum,
                        form_values=form_values)
    return templates.TemplateResponse(request, "partials/transactions_root.html", ctx, status_code=status_code)


def _months_context(
    db: Session, *, q: str | None, account_id: int | None, category_id: int | None,
    kind: str | None, only_receipts: bool, before: date, zeitraum: str,
) -> dict:
    """Kontext fürs Nachladen älterer Monatskarten (``partials/tx_months.html``)."""
    zeitraum = _zeitraum_wert(zeitraum)
    txs, next_before = _filtered_transactions(
        db, q=q, account_id=account_id, category_id=category_id, kind=kind,
        only_receipts=only_receipts, before=before,
        zeit_conds=_zeitraum_bedingungen(zeitraum, heute_lokal()),
    )
    groups = _group_by_month(txs)
    tx_ids = [t.id for t in txs]
    attachments_by_tx = _attachments_by_tx(db, tx_ids)
    accounts = _active_accounts(db)
    lohn_by_tx = lohn.aufstellungen_zu(db, txs)
    return {
        "month_groups": groups,
        "next_before": next_before,
        "from_loadmore": True,  # nachgeladene Monatskarten nicht auto-öffnen
        "primary_acc_id": accounts[0].id if accounts else None,
        "attachments_by_tx": attachments_by_tx,
        "attachment_tx_ids": set(attachments_by_tx.keys()),
        "splits_by_tx": _splits_by_tx(db, tx_ids),
        # Auch nachgeladene Monatskarten müssen die Aufstellung zeigen können —
        # sonst hinge die Aufschlüsselung daran, wie weit man gescrollt hat.
        "lohn_by_tx": lohn_by_tx,
        "lohn_proben": lohn.jahresproben(
            db, {t.date.year for t in txs if t.id in lohn_by_tx}
        ),
        "today": heute_lokal().isoformat(),
        "filter": {
            "q": q or "", "account_id": account_id or "",
            "category_id": category_id or "", "kind": kind or "",
            "only_receipts": only_receipts, "zeitraum": zeitraum,
        },
    }


# ---------------------------------------------------------------------------
# Anzeige
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    form: str = "none",
    id: int | None = None,
    q: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    kind: str | None = None,
    only_receipts: int = 0,
    before: str | None = None,
    zeitraum: str = ZEITRAUM_VORGABE,
) -> Response:
    """Buchungs-Seite mit Filter (Konto/Kategorie/Typ/Text/nur-mit-Beleg), Monats-Gruppierung.

    ``form=new`` oder ``form=edit&id=..`` öffnet das Formular. Den früheren
    Parameter ``quickcat=<id>`` gibt es nicht mehr: der Kategorie-Picker klappt
    heute lokal auf (ein gemeinsames Panel in der Shell) statt über einen
    Roundtrip, der die ganze Liste neu rendert.
    ``account_id``/``category_id`` kommen als String (leer = „Alle …") und werden
    via :func:`_opt_int` zu int|None — sonst 422 auf den leeren Default-Filter.
    ``before=YYYY-MM-DD`` (nur HTMX) lädt die nächsten älteren Monatskarten nach.
    ``zeitraum`` begrenzt Summenzeile UND Liste: fehlt er, gilt das laufende
    Jahr; ``monat`` engt auf den laufenden Monat ein, ``alles`` zeigt den
    Gesamtbestand. Die Vorgabe steht hier und NUR hier: bei mehreren Jahren
    Buchungen öffnete die Seite sonst mit allem auf einmal.
    """
    edit_tx = None
    if form == "edit" and id is not None:
        edit_tx = db.get(Transaction, id)
        if edit_tx is None:
            form = "none"
    only_rcpt = bool(only_receipts)
    acc_id = _opt_int(account_id)
    cat_id = _opt_int(category_id)

    if before and request.headers.get("HX-Request") == "true":
        try:
            before_date = date.fromisoformat(before)
        except ValueError:
            before_date = None
        if before_date is not None:
            ctx = _months_context(db, q=q, account_id=acc_id, category_id=cat_id,
                                  kind=kind, only_receipts=only_rcpt, before=before_date,
                                  zeitraum=zeitraum)
            return templates.TemplateResponse(request, "partials/tx_months.html", ctx)

    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db, form_mode=form, edit_tx=edit_tx,
                            q=q, account_id=acc_id, category_id=cat_id, kind=kind,
                            only_receipts=only_rcpt, zeitraum=zeitraum)

    ctx = _base_context(db, form_mode=form, edit_tx=edit_tx, error=None,
                        q=q, account_id=acc_id, category_id=cat_id, kind=kind,
                        only_receipts=only_rcpt, zeitraum=zeitraum)
    ctx |= {"user": user, "active_tab": "transactions"}
    return templates.TemplateResponse(request, "transactions.html", ctx)


@router.post("/{tx_id:int}/category", response_class=HTMLResponse)
def quick_set_category(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    assign_category_id: Annotated[str, Form()] = "",
    q: Annotated[str | None, Form()] = None,
    account_id: Annotated[str | None, Form()] = None,
    category_id: Annotated[str | None, Form()] = None,
    kind: Annotated[str | None, Form()] = None,
    only_receipts: Annotated[str, Form()] = "0",
    zeitraum: Annotated[str, Form()] = ZEITRAUM_VORGABE,
) -> Response:
    """Setzt die Kategorie EINER Buchung direkt aus der Liste (Inline-Picker),
    ohne das volle Bearbeiten-Formular zu öffnen — spart Klicks beim häufigsten
    Task. Der aktive Filter bleibt erhalten (Felder werden mitgesendet).

    Der Vertrag zum Browser ist unverändert: das Hidden-Input des Auslösers heisst
    ``assign_category_id`` — nicht ``category_id``, so heisst der FILTER, der per
    ``hx-include`` mitreist — und feuert bei der Auswahl das change-Event, an dem
    dieses ``hx-post`` hängt."""

    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(
            request, db, error="Buchung nicht gefunden.", status_code=404,
            q=q or None, account_id=_opt_int(account_id), category_id=_opt_int(category_id),
            kind=kind or None, only_receipts=bool(_opt_int(only_receipts)), zeitraum=zeitraum,
        )
    cat = db.get(Category, int(assign_category_id)) if assign_category_id.strip().isdigit() else None
    tx.category_id = cat.id if cat else None
    tx.management_type = cat.management_type if cat else None
    db.commit()
    # keep_visible: die eben zugeordnete Zeile muss in der Antwort vorkommen —
    # auch wenn sie in einem nachgeladenen, älteren Monat steht.
    return _render_root(
        request, db, keep_visible=tx.id,
        q=q or None, account_id=_opt_int(account_id), category_id=_opt_int(category_id),
        kind=kind or None, only_receipts=bool(_opt_int(only_receipts)), zeitraum=zeitraum,
    )


# ---------------------------------------------------------------------------
# Massen-Zuweisung: alle Treffer des aktuellen Filters einer Kategorie zuordnen
# ---------------------------------------------------------------------------


def _bulk_undo_trigger(
    resp: Response, *, payload: str, message: str, filter_args: dict, zeitraum: str,
) -> Response:
    """Hängt den ``moneten:toast``-Trigger mit „Rückgängig" an (Muster aus
    ``routers/rules.py``, dort ``_undo_trigger``).

    Zusätzlich zum Vorzustand reisen die Filterfelder mit: die Rücknahme rendert
    ``#transactions-root`` neu, und ohne den Filter stünde danach die
    **ungefilterte** Liste da, während das ``hx-preserve``-Suchfeld noch den
    Suchtext zeigt — derselbe Fehler, den schon das Löschen einer Buchung hatte.
    """
    if not payload:
        return resp
    resp.headers["HX-Trigger"] = json.dumps({
        "moneten:toast": {
            "message": message,
            "undo": {
                "url": "/transactions/assign-undo",
                "target": "#transactions-root", "swap": "innerHTML",
                "values": {
                    "undo": payload,
                    "q": filter_args["q"] or "",
                    "account_id": str(filter_args["account_id"] or ""),
                    "category_id": str(filter_args["category_id"] or ""),
                    "kind": filter_args["kind"] or "",
                    "only_receipts": "1" if filter_args["only_receipts"] else "0",
                    "zeitraum": zeitraum,
                },
            },
        }
    })
    return resp


@router.get("/bulk-bar", response_class=HTMLResponse)
def bulk_bar(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    kind: str | None = None,
    only_receipts: str = "0",
    overwrite: str = "0",
    assign_category_id: str = "",
    zeitraum: str = "alles",
) -> Response:
    """Rendert NUR die Massen-Zuweisungs-Leiste neu — für den Umschalter
    „bereits zugeordnete überschreiben".

    ``zeitraum`` hat hier bewusst die Vorgabe ``"alles"`` und NICHT die der
    Seite: wer diese Teilansicht ohne das Feld aufruft, bekommt exakt die
    Bedingungen von früher. Die Vorschau-Zahl darf sich nicht heimlich
    verkleinern, nur weil anderswo ein Anzeige-Vorgabewert eingeführt wurde —
    die Seite schickt das Feld immer mit (``hx-include="#tx-filter"``).

    Bewusst eine Server-Route statt eines JS-Umschalters: Vorschau-Zahl,
    Summe und der Text der Sicherheitsabfrage stammen so garantiert aus
    derselben Query wie die spätere Zuweisung. Ein Zähler, den JS aus zwei
    vorgerenderten Zahlen umschaltet, wäre genau die Stelle, an der Vorschau und
    Wirkung auseinanderdriften. ``assign_category_id`` reist mit, damit die
    schon gewählte Zielkategorie den Swap überlebt.
    """
    fa = _filter_args(q=q, account_id=account_id, category_id=category_id,
                      kind=kind, only_receipts=only_receipts)
    # DIESELBE Sperre wie in _base_context und assign_filtered: ohne wirksamen
    # Filter gibt es keine Leiste. Sie fehlte hier zuerst — die Teilansicht liess
    # sich direkt aufrufen und bot dann die Zuweisung auf den GANZEN Bestand an,
    # obwohl die Vollseite sie zu Recht verweigerte. Eine Sperre, die nur an
    # einem von drei Eingängen hängt, ist keine.
    if not _filter_conditions(**fa):
        return HTMLResponse("")
    return templates.TemplateResponse(request, "partials/tx_bulk.html", {
        "bulk": _bulk_context(db, fa, overwrite=(overwrite == "1"),
                              assign_category_id=assign_category_id,
                              zeitraum=_zeitraum_wert(zeitraum)),
        "category_groups": _category_groups(db),
        "filter": {"q": fa["q"] or ""},
    })


@router.post("/assign-filtered", response_class=HTMLResponse)
def assign_filtered(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    assign_category_id: Annotated[str, Form()] = "",
    overwrite: Annotated[str, Form()] = "0",
    q: Annotated[str | None, Form()] = None,
    account_id: Annotated[str | None, Form()] = None,
    category_id: Annotated[str | None, Form()] = None,
    kind: Annotated[str | None, Form()] = None,
    only_receipts: Annotated[str, Form()] = "0",
    zeitraum: Annotated[str, Form()] = "alles",
) -> Response:
    """Ordnet ALLE Buchungen des aktuellen Filters einer Kategorie zu.

    Die Hauptfalle steht in :mod:`moneten.services.bulk_assign`: die Liste ist
    paginiert (``before=``), die Zuweisung darf aber weder nur die geladene
    erste Seite treffen noch irgendetwas ausserhalb des Filters. Deshalb wird
    hier NICHT über die gerenderten Buchungen gelaufen, sondern die
    Filterbedingung erneut gebaut — dieselbe Funktion, die auch die Vorschau
    zählt (:func:`_filter_conditions` + :func:`bulk_conditions`), ohne
    Monatsfenster und ohne Limit.

    ``zeitraum`` hat hier — anders als auf der Seite — die Vorgabe ``"alles"``.
    Das ist Absicht und der heikelste Punkt der Jahres-Vorgabe: ein
    Anzeige-Vorgabewert darf eine Massenaktion nicht heimlich umdefinieren. Ein
    Aufruf ohne dieses Feld trifft deshalb exakt dieselbe Menge wie vor der
    Änderung. Die Seite schickt es immer mit — eingefroren im ``hx-vals`` des
    Knopfes, aus demselben Aufruf, aus dem auch die bestätigte Zahl stammt
    (:func:`_bulk_context`).

    ``overwrite="1"`` fasst auch bereits kategorisierte Buchungen an. Ohne diese
    ausdrückliche Freigabe bleiben sie unberührt — der Bestand verspricht an
    mehreren Stellen, manuell gesetzte Kategorien nie zu überschreiben, und eine
    Sammelaktion ist der schlechteste Ort, dieses Versprechen still zu brechen.
    """
    fa = _filter_args(q=q, account_id=account_id, category_id=category_id,
                      kind=kind, only_receipts=only_receipts)
    # DERSELBE Normalisierer wie in /bulk-bar. Nur der Vorgabewert des Parameters
    # unterscheidet sich („alles" statt „jahr"); würden die beiden Routen einen
    # Wert unterschiedlich auslegen, spräche die Vorschau wieder von einer anderen
    # Menge als der Knopf.
    zeitraum = _zeitraum_wert(zeitraum)
    # Der Schutz hängt an den WIRKLICH erzeugten Bedingungen, nicht an den rohen
    # Formularwerten: ein Feld, das keine Bedingung ergibt (unbekanntes ``kind``,
    # Suchtext aus Leerzeichen), sah sonst wie ein Filter aus — und öffnete die
    # Zuweisung auf den ganzen Bestand. Der Zeitraum zählt bewusst NICHT mit:
    # „alles aus 2026" ist kein Filter, sondern immer noch der ganze Bestand.
    basis_conds = _filter_conditions(**fa)
    if not basis_conds:
        return _render_root(request, db, status_code=400,
                            zeitraum=zeitraum, **fa,
                            error="Massen-Zuweisung braucht einen aktiven Filter — "
                                  "sonst wäre „alle Treffer“ der ganze Bestand.")
    cat = db.get(Category, int(assign_category_id)) if assign_category_id.strip().isdigit() else None
    if cat is None:
        return _render_root(request, db, status_code=400,
                            zeitraum=zeitraum, **fa,
                            error="Bitte zuerst eine Zielkategorie wählen.")

    # Zeitraum NACH der Schutzabfrage dazu: er darf keinen Filter ersetzen, muss
    # die Menge aber begrenzen — die Vorschau hat mit derselben Grenze gezählt.
    basis_conds = [*basis_conds, *_zeitraum_bedingungen(zeitraum, heute_lokal())]
    conds = bulk_conditions(basis_conds)
    if overwrite != "1":
        conds = [*conds, Transaction.category_id.is_(None)]

    # Vorzustand VOR der Änderung lesen: liefert die Zahl fürs Ergebnis und die
    # Nutzlast fürs Rückgängig. Nur drei Spalten statt hydrierter ORM-Objekte —
    # der Filter kann tausende Zeilen treffen.
    rows = [
        (r[0], r[1], r[2]) for r in db.execute(
            select(Transaction.id, Transaction.category_id, Transaction.management_type)
            .where(*conds)
        ).all()
    ]
    if not rows:
        return _render_root(request, db, zeitraum=zeitraum, **fa,
                            info="Keine Buchung im Filter, die zugeordnet werden könnte.")

    # management_type folgt der Kategorie — genau wie beim Einzel-Picker
    # (:func:`quick_set_category`) und beim Formular (:func:`_apply_form`).
    # Transfer-Unterkategorien tragen selbst ``TRANSFER``, damit fällt eine so
    # zugeordnete Buchung korrekt aus Ausgaben/Budget/Sankey heraus.
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), _IN_LIMIT):
        db.execute(
            update(Transaction).where(Transaction.id.in_(ids[i:i + _IN_LIMIT]))
            .values(category_id=cat.id, management_type=cat.management_type)
        )
    db.commit()

    payload = pack_undo(rows) if len(rows) <= BULK_UNDO_MAX else ""
    msg = f"{len(rows)} Buchung(en) → {cat.name} zugeordnet."
    resp = _render_root(request, db, zeitraum=zeitraum, **fa, info=msg)
    return _bulk_undo_trigger(resp, payload=payload, message=msg, filter_args=fa,
                              zeitraum=zeitraum)


@router.post("/assign-undo", response_class=HTMLResponse)
def assign_undo(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    undo: Annotated[str, Form()] = "",
    q: Annotated[str | None, Form()] = None,
    account_id: Annotated[str | None, Form()] = None,
    category_id: Annotated[str | None, Form()] = None,
    kind: Annotated[str | None, Form()] = None,
    only_receipts: Annotated[str, Form()] = "0",
    zeitraum: Annotated[str, Form()] = "alles",
) -> Response:
    """Nimmt eine Massen-Zuweisung zurück — buchungsgenau.

    Anders als ``/rules/undo``, das alle betroffenen Buchungen pauschal wieder
    auf „offen" setzt, wird hier der **einzelne** Vorzustand wiederhergestellt.
    Das ist der Unterschied, der bei gemischten Ausgangslagen zählt: waren im
    Filter 180 offene und 15 bereits zugeordnete Buchungen, hätte ein pauschales
    „alle auf offen" die 15 alten Kategorien vernichtet — beim Reparieren eines
    Fehlklicks einen zweiten, grösseren Schaden angerichtet.
    """
    n = 0
    for ids, cat_id, mgmt in unpack_undo(undo):
        for i in range(0, len(ids), _IN_LIMIT):
            teil = ids[i:i + _IN_LIMIT]
            db.execute(
                update(Transaction).where(Transaction.id.in_(teil))
                .values(category_id=cat_id, management_type=mgmt)
            )
            n += len(teil)
    if n:
        db.commit()
    fa = _filter_args(q=q, account_id=account_id, category_id=category_id,
                      kind=kind, only_receipts=only_receipts)
    # Der Zeitraum wirkt hier nur auf die Anzeige — zurückgesetzt wird über die
    # gespeicherten IDs, nicht über eine Bedingung.
    return _render_root(request, db, zeitraum=zeitraum, **fa,
                        info=f"Rückgängig: {n} Buchung(en) auf den vorherigen Stand zurückgesetzt.")


# ---------------------------------------------------------------------------
# Hilfsfunktion: Formulardaten validieren und auf eine Transaktion anwenden
# ---------------------------------------------------------------------------


def _apply_form(
    db: Session,
    tx: Transaction,
    *,
    kind: str,
    amount_raw: str,
    date_raw: str,
    account_id: int,
    category_id: int | None,
    description: str,
    notes: str,
) -> str | None:
    """Setzt die Felder von ``tx`` aus den Formulardaten. Gibt Fehlertext oder None.

    ``tx`` ist entweder neu (noch nicht in der DB) oder bestehend.
    """
    account = db.get(Account, account_id)
    if account is None:
        return "Bitte ein gültiges Konto wählen."

    try:
        betrag = parse_amount(amount_raw)
    except InvalidOperation:
        return "Betrag ist keine gültige Zahl."
    if betrag <= 0:
        return "Bitte einen Betrag grösser als 0 eingeben."

    try:
        buchungsdatum = date.fromisoformat(date_raw)
    except (ValueError, TypeError):
        return "Bitte ein gültiges Datum wählen."

    category = db.get(Category, category_id) if category_id else None

    tx.account_id = account_id
    tx.category_id = category.id if category else None
    tx.date = buchungsdatum
    # Vorzeichen aus Typ: Einnahme positiv, Ausgabe negativ.
    tx.amount = betrag if kind == "einnahme" else -betrag
    tx.description = description.strip()
    tx.notes = notes.strip() or None
    # management_type aus Kategorie übernehmen (für spätere Auswertungen).
    tx.management_type = category.management_type if category else None
    return None


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------


@router.post("", response_class=HTMLResponse)
def create_transaction(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    kind: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    date_: Annotated[str, Form(alias="date")],
    account_id: Annotated[int, Form()],
    category_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Erfasst eine neue Buchung."""
    tx = Transaction(account_id=account_id, date=heute_lokal(), amount=0)  # Platzhalter, gleich überschrieben
    error = _apply_form(
        db, tx,
        kind=kind, amount_raw=amount, date_raw=date_,
        account_id=account_id, category_id=int(category_id) if category_id else None,
        description=description, notes=notes,
    )
    if error:
        _roh = {"kind": kind, "amount": amount, "date": date_,
                "account_id": account_id, "category_id": category_id,
                "description": description, "notes": notes}
        return _render_root(request, db, form_mode="new", error=error, status_code=400,
                            form_values=_roh)

    db.add(tx)
    db.flush()
    recalc_account_balance(db, tx.account_id)
    db.commit()
    # keep_visible: die eben erfasste Buchung muss in der Antwort stehen. Bei
    # einem nachgetragenen Datum (letztes Jahr, alter Beleg) läge sie sonst
    # ausserhalb von Monatsfenster und Jahres-Vorgabe — man speichert und sieht
    # nichts, was von „nicht gespeichert" nicht zu unterscheiden ist.
    return _render_root(request, db, form_mode="none", keep_visible=tx.id)


# ---------------------------------------------------------------------------
# Bearbeiten
# ---------------------------------------------------------------------------


@router.post("/{tx_id:int}", response_class=HTMLResponse)
def update_transaction(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    kind: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    date_: Annotated[str, Form(alias="date")],
    account_id: Annotated[int, Form()],
    category_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    learn_rule: Annotated[str, Form()] = "",
    learn_keyword: Annotated[str, Form()] = "",
) -> Response:
    """Aktualisiert eine bestehende Buchung.

    Optional (``learn_rule``): aus der manuell gewählten Kategorie eine Regel
    lernen und gleich auf alle weiteren unkategorisierten Buchungen mit demselben
    Stichwort anwenden. Läuft serverseitig — die App lernt selbständig dazu.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)

    old_account_id = tx.account_id
    was_split = tx.is_split
    old_amount = tx.amount
    cat_id = int(category_id) if category_id else None
    error = _apply_form(
        db, tx,
        kind=kind, amount_raw=amount, date_raw=date_,
        account_id=account_id, category_id=cat_id,
        description=description, notes=notes,
    )
    if error:
        _roh = {"kind": kind, "amount": amount, "date": date_,
                "account_id": account_id, "category_id": category_id,
                "description": description, "notes": notes}
        return _render_root(request, db, form_mode="edit", edit_tx=tx, error=error,
                            status_code=400, form_values=_roh)

    # Aufgeteilte Buchung: die Einzel-Kategorie aus dem Formular zählt nicht —
    # die Splits bestimmen die Kategorien. Ändert sich der Betrag, passen die
    # Splits nicht mehr → Aufteilung aufheben (mit Hinweis).
    split_cleared = False
    if was_split:
        if tx.amount == old_amount:
            tx.is_split = True
            tx.category_id = None
            tx.management_type = None
        else:
            tx.splits.clear()  # cascade delete-orphan entfernt die Anteile
            tx.is_split = False
            split_cleared = True

    db.add(tx)
    db.flush()
    # Beide betroffenen Konten neu rechnen (Konto kann gewechselt haben).
    recalc_account_balance(db, old_account_id)
    if tx.account_id != old_account_id:
        recalc_account_balance(db, tx.account_id)
    db.commit()

    info = "Aufteilung aufgehoben (Betrag geändert)." if split_cleared else None
    if learn_rule == "1" and cat_id is not None and learn_keyword.strip():
        created, applied = learn_from_transaction(
            db, keyword=learn_keyword, category_id=cat_id, source_tx_id=tx_id
        )
        if created or applied:
            teile = []
            if created:
                teile.append(f"Regel „{learn_keyword.strip().lower()}“ gelernt")
            if applied:
                teile.append(f"{applied} weitere Buchung(en) zugeordnet")
            info = " · ".join(teile) + "."
    return _render_root(request, db, form_mode="none", info=info, keep_visible=tx_id)


# ---------------------------------------------------------------------------
# Aufteilung (Auto-Split): eine Buchung auf mehrere Kategorie-Anteile verteilen
# ---------------------------------------------------------------------------


def _parse_split_rows(cats: list[str], amounts: list[str]) -> tuple[list[tuple[int, Decimal]], str | None]:
    """Validiert die Editor-Zeilen → Liste ``(category_id, positiver Betrag)``.

    Leere Zeilen (weder Kategorie noch Betrag) werden ignoriert. Fehlt bei einer
    Position die Kategorie oder ist der Betrag ungültig/≤0, gibt es einen
    Fehlertext (zweiter Rückgabewert)."""
    rows: list[tuple[int, Decimal]] = []
    for cat_raw, amt_raw in zip(cats, amounts, strict=False):
        amt = (amt_raw or "").strip()
        cat = (cat_raw or "").strip()
        if not amt and not cat:
            continue
        if not cat:
            return [], "Jede Position braucht eine Kategorie."
        try:
            betrag = parse_amount(amt)
        except InvalidOperation:
            return [], "Ein Betrag ist keine gültige Zahl."
        if betrag <= 0:
            return [], "Jeder Anteil muss grösser als 0 sein."
        rows.append((int(cat), betrag))
    return rows, None


@router.post("/{tx_id:int}/split", response_class=HTMLResponse)
def save_split(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    split_cat: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI-Form-Default
    split_amount: Annotated[list[str], Form()] = [],  # noqa: B006
) -> Response:
    """Speichert die Aufteilung einer Buchung in mehrere Kategorie-Anteile.

    Die Summe der Anteile muss dem Buchungsbetrag entsprechen. Der Saldo bleibt
    unberührt — verteilt wird nur die Kategorie-Zuordnung. Bei genau einer
    Kategorie wird statt einer Aufteilung einfach die Kategorie gesetzt.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    if tx.management_type == ManagementType.TRANSFER:
        return _render_root(request, db, form_mode="edit", edit_tx=tx,
                            error="Umbuchungen lassen sich nicht aufteilen.", status_code=400)

    entered = [{"category_id": c or "", "amount": a or ""}
               for c, a in zip(split_cat, split_amount, strict=False)]
    rows, err = _parse_split_rows(split_cat, split_amount)
    if err:
        return _render_root(request, db, form_mode="edit", edit_tx=tx, error=err,
                            split_rows=entered, status_code=400)

    target = tx.amount.copy_abs()
    if not rows:
        tx.splits.clear()
        tx.is_split = False
        db.add(tx)
        db.commit()
        return _render_root(request, db, form_mode="edit", edit_tx=tx, info="Aufteilung aufgehoben.")

    total = sum((b for _, b in rows), Decimal("0"))
    if (total - target).copy_abs() > Decimal("0.005"):
        rest = target - total
        return _render_root(
            request, db, form_mode="edit", edit_tx=tx, split_rows=entered, status_code=400,
            error=(f"Summe der Aufteilung (CHF {total:.2f}) muss dem Buchungsbetrag "
                   f"(CHF {target:.2f}) entsprechen. Rest: CHF {rest:.2f}."),
        )

    sign = Decimal("-1") if tx.amount < 0 else Decimal("1")
    if len(rows) == 1:
        cid, _betrag = rows[0]
        cat = db.get(Category, cid)
        tx.splits.clear()
        tx.is_split = False
        tx.category_id = cid
        tx.management_type = cat.management_type if cat else None
        info = "Kategorie gesetzt — bei nur einer Kategorie ist keine Aufteilung nötig."
    else:
        tx.splits.clear()
        db.flush()
        for cid, betrag in rows:
            db.add(TransactionSplit(transaction_id=tx.id, category_id=cid, amount=betrag * sign))
        tx.is_split = True
        tx.category_id = None
        tx.management_type = None
        info = f"Aufteilung gespeichert: {len(rows)} Kategorien."

    db.add(tx)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx, info=info)


@router.post("/{tx_id:int}/split/clear", response_class=HTMLResponse)
def clear_split(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
) -> Response:
    """Hebt die Aufteilung einer Buchung wieder auf (Anteile entfernen)."""
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    tx.splits.clear()
    tx.is_split = False
    db.add(tx)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx, info="Aufteilung aufgehoben.")


@router.post("/{tx_id:int}/split/suggest", response_class=HTMLResponse)
def suggest_split(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
) -> Response:
    """Schlägt eine Aufteilung aus dem Belegtext vor (nicht gespeichert).

    Das Ergebnis wird als vorbefüllte, noch nicht gespeicherte Editor-Zeilen
    angezeigt — der Nutzer prüft, korrigiert und speichert anschliessend.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    att = db.scalar(
        select(Attachment).where(
            Attachment.transaction_id == tx.id, Attachment.ocr_text.is_not(None)
        )
    )
    result = suggest_splits(db, tx, att.ocr_text if att else None)
    rows = [{"category_id": r["category_id"], "amount": f"{r['amount']:.2f}"}
            for r in result["rows"]]
    if not rows:
        rows = _split_rows_for(tx)
    return _render_root(request, db, form_mode="edit", edit_tx=tx,
                        split_rows=rows, split_note=result["note"])


# ---------------------------------------------------------------------------
# Lohnzusammensetzung: Bruttolohn, Abzüge, Nettolohn an der Gutschrift
# ---------------------------------------------------------------------------


@router.post("/{tx_id:int}/lohn", response_class=HTMLResponse)
def save_lohn(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    lohn_art: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI-Form-Default
    lohn_label: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_betrag: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_alt: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_herkunft: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_grundlage: Annotated[str, Form()] = "",
) -> Response:
    """Speichert die Zusammensetzung einer Lohn-Gutschrift.

    **Ausdrücklich ohne Abgleich auf den gebuchten Betrag.** Die Aufstellung
    darf vom Betrag der Buchung abweichen — bei aus Jahreswerten geschätzten
    Posten tut sie das fast immer. Eine Prüfung „Summe muss stimmen" (wie bei
    der Kategorie-Aufteilung, wo sie richtig ist) würde hier zum Zurechtbiegen
    der Zahlen zwingen. Die Differenz steht stattdessen in der Anzeige.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    if not lohn.darf_aufschluesseln(tx):
        return _render_root(request, db, form_mode="edit", edit_tx=tx, status_code=400,
                            error="Nur Einnahmen lassen sich als Lohn aufschlüsseln.")

    # ``alt`` MUSS mit zurück ins Formular. Ohne dieses Feld schrieb das Template
    # den frisch getippten Wert als „alt" — nach einem Validierungsfehler
    # verglich die Herkunfts-Prüfung die Zahl also mit sich selbst und liess eine
    # selbst getippte Zahl als „gerechnet" durchgehen, mit „≈" in der Anzeige.
    eingetippt = [
        {"label": label, "art": art, "betrag": betrag, "alt": alt, "herkunft": herkunft}
        for art, label, betrag, alt, herkunft in zip(
            lohn_art, lohn_label, lohn_betrag, lohn_alt, lohn_herkunft, strict=False
        )
    ]
    rows, err = _lohn_rows_geprueft(lohn_art, lohn_label, lohn_betrag, lohn_alt, lohn_herkunft)
    grundlage = lohn_grundlage.strip()[:160] or None
    if err:
        return _render_root(request, db, form_mode="edit", edit_tx=tx, error=err,
                            lohn_rows=eingetippt, lohn_grundlage=grundlage, status_code=400)

    if not rows:
        geloescht = lohn.entfernen(db, tx.id)
        db.commit()
        return _render_root(request, db, form_mode="edit", edit_tx=tx,
                            info="Lohnzusammensetzung entfernt." if geloescht
                                 else "Nichts erfasst — keine Lohnzusammensetzung angelegt.")

    lohn.speichern(db, tx, rows, grundlage=grundlage)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx,
                        info=f"Lohnzusammensetzung gespeichert: {len(rows)} Positionen.")


@router.post("/{tx_id:int}/lohn/clear", response_class=HTMLResponse)
def clear_lohn(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
) -> Response:
    """Entfernt die Lohnzusammensetzung einer Buchung. Die Buchung bleibt."""
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    lohn.entfernen(db, tx.id)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx,
                        info="Lohnzusammensetzung entfernt.")


@router.post("/{tx_id:int}/lohn/probe", response_class=HTMLResponse)
def probe_lohn(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    lohn_art: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI-Form-Default
    lohn_label: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_betrag: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_alt: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_herkunft: Annotated[list[str], Form()] = [],  # noqa: B006
    lohn_key: Annotated[list[str], Form()] = [],  # noqa: B006
) -> Response:
    """Die mitlaufende Gegenprobe des Editors — **speichert nichts**.

    Sie beantwortet dieselbe Frage wie das Speichern eine Sekunde später und
    beantwortet sie mit demselben Code: erst ``_lohn_rows_geprueft``, dann
    ``lohn.aufstellung_aus_posten``, gerendert mit dem Baustein, den auch der
    Aufklapper an der Buchung benutzt. Gerechnet wurde das vorher im Browser —
    die Vorschau zeigte deshalb ein Ergebnis auch dort, wo diese Route ablehnt,
    und sie schrieb die Differenz ohne Vorzeichen, während der Aufklapper eines
    setzte. Zwei Darstellungen derselben Zahl, und die stumme war die, in der
    der Nutzer entscheidet.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None or not lohn.darf_aufschluesseln(tx):
        return HTMLResponse("")

    rows, hinweis = _lohn_rows_geprueft(lohn_art, lohn_label, lohn_betrag, lohn_alt, lohn_herkunft)
    aufstellung = None
    if hinweis is None:
        if rows:
            aufstellung = lohn.aufstellung_aus_posten(rows, tx.amount, grundlage=None)
        elif lohn.abrechnung_zu(db, tx.id) is not None:
            hinweis = "Ohne Beträge wird die erfasste Zusammensetzung entfernt."
        else:
            hinweis = "Noch kein Betrag erfasst."
    return templates.TemplateResponse(request, "partials/lohn_probe.html", {
        "a": aufstellung,
        "lohn_hinweis": hinweis,
        "lohn_marken": _lohn_marken(lohn_key, lohn_betrag, lohn_alt, lohn_herkunft),
    })


@router.post("/{tx_id:int}/lohn/suggest", response_class=HTMLResponse)
def suggest_lohn(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
) -> Response:
    """Füllt den Editor mit dem, was sich ableiten lässt — **ungespeichert**.

    Wie beim Auto-Split aus dem Beleg: der Vorschlag ist ein Angebot, das der
    Nutzer prüft und korrigiert. Was er unverändert lässt, bleibt beim Speichern
    als „gerechnet" gekennzeichnet.
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)
    rows, grundlage = lohn.vorschlag(db, tx)
    return _render_root(request, db, form_mode="edit", edit_tx=tx,
                        lohn_rows=rows, lohn_grundlage=grundlage)


# ---------------------------------------------------------------------------
# Löschen
# ---------------------------------------------------------------------------


@router.post("/{tx_id:int}/delete", response_class=HTMLResponse)
def delete_transaction(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    q: Annotated[str | None, Form()] = None,
    account_id: Annotated[str | None, Form()] = None,
    category_id: Annotated[str | None, Form()] = None,
    kind: Annotated[str | None, Form()] = None,
    only_receipts: Annotated[str, Form()] = "0",
    zeitraum: Annotated[str, Form()] = ZEITRAUM_VORGABE,
) -> Response:
    """Löscht eine Buchung und rechnet den Konto-Saldo neu.

    Gehört die Buchung zu einem Transfer (``transfer_group_id``), werden
    BEIDE Seiten gelöscht und alle betroffenen Konten neu gerechnet.
    Der aktive Filter kommt per ``hx-include="#tx-filter"`` mit und bleibt
    erhalten — sonst zeigte die Antwort die ungefilterte Liste, während das
    hx-preserve-Suchfeld noch den alten Suchtext anzeigt.
    """
    tx = db.get(Transaction, tx_id)
    if tx is not None:
        affected: set[int] = set()
        to_delete = (
            list(db.scalars(select(Transaction).where(Transaction.transfer_group_id == tx.transfer_group_id)))
            if tx.transfer_group_id
            else [tx]
        )
        for victim in to_delete:
            affected.add(victim.account_id)
            # Nur die Buchung löschen — Quittungs-Dateien im Ordner bleiben
            # unangetastet (sie gehören dem Nutzer); Attachment-Records gehen
            # per CASCADE.
            db.delete(victim)
        db.flush()
        for acc_id in affected:
            recalc_account_balance(db, acc_id)
        db.commit()
    return _render_root(
        request, db, form_mode="none",
        q=q or None, account_id=_opt_int(account_id), category_id=_opt_int(category_id),
        kind=kind or None, only_receipts=bool(_opt_int(only_receipts)), zeitraum=zeitraum,
    )


# ---------------------------------------------------------------------------
# Transfer (Umbuchung zwischen eigenen Konten)
# ---------------------------------------------------------------------------


def _transfer_category(db: Session, to_account: Account) -> Category | None:
    """Passende Transfer-Kategorie: Bargeldbezug bei Bargeld-Ziel, sonst Kontoübertrag."""
    name = "Bargeldbezug" if to_account.type == AccountType.CASH else "Kontoübertrag"
    return db.scalar(select(Category).where(Category.name == name))


@router.post("/transfer", response_class=HTMLResponse)
def create_transfer(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    from_account_id: Annotated[int, Form()],
    to_account_id: Annotated[int, Form()],
    amount: Annotated[str, Form()],
    date_: Annotated[str, Form(alias="date")],
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Bucht eine Umbuchung: −Betrag auf Quellkonto, +Betrag auf Zielkonto.

    Beide Buchungen teilen eine ``transfer_group_id`` und tragen
    ``management_type=TRANSFER`` (damit sie aus Einnahmen/Ausgaben-Auswertungen
    herausfallen).
    """
    if from_account_id == to_account_id:
        return _render_root(request, db, form_mode="transfer",
                            error="Quell- und Zielkonto müssen unterschiedlich sein.", status_code=400)
    src = db.get(Account, from_account_id)
    dst = db.get(Account, to_account_id)
    if src is None or dst is None:
        return _render_root(request, db, form_mode="transfer",
                            error="Bitte gültige Konten wählen.", status_code=400)
    try:
        betrag = parse_amount(amount)
    except InvalidOperation:
        return _render_root(request, db, form_mode="transfer",
                            error="Betrag ist keine gültige Zahl.", status_code=400)
    if betrag <= 0:
        return _render_root(request, db, form_mode="transfer",
                            error="Bitte einen Betrag grösser als 0 eingeben.", status_code=400)
    try:
        buchungsdatum = date.fromisoformat(date_)
    except (ValueError, TypeError):
        return _render_root(request, db, form_mode="transfer",
                            error="Bitte ein gültiges Datum wählen.", status_code=400)

    group_id = uuid.uuid4().hex
    cat = _transfer_category(db, dst)
    note = notes.strip() or None

    db.add(Transaction(
        account_id=src.id, category_id=cat.id if cat else None, date=buchungsdatum,
        amount=-betrag, description=f"Umbuchung an {dst.name}", notes=note,
        management_type=ManagementType.TRANSFER, transfer_group_id=group_id,
    ))
    db.add(Transaction(
        account_id=dst.id, category_id=cat.id if cat else None, date=buchungsdatum,
        amount=betrag, description=f"Umbuchung von {src.name}", notes=note,
        management_type=ManagementType.TRANSFER, transfer_group_id=group_id,
    ))
    db.flush()
    recalc_account_balance(db, src.id)
    recalc_account_balance(db, dst.id)
    db.commit()
    return _render_root(request, db, form_mode="none")


# ---------------------------------------------------------------------------
# Quittungen / Anhänge
# ---------------------------------------------------------------------------


def _run_ocr(att: Attachment) -> None:
    """Liest den Beleg neu ein und speichert das Ergebnis am Attachment.

    Dieselbe Funktion wie beim Zuordnen (``read_receipt_data``) — sonst schriebe
    „Text neu auslesen" die geprüften Positionen einer Rechnung mit einem aus dem
    Text geschätzten Betrag zu. Tut nichts, wenn kein gültiger Pfad vorliegt."""
    if not att.file_path:
        return
    resolved = resolve_receipt(att.file_path)
    if resolved is None:
        return
    read_receipt_data(att, str(resolved))


@router.post("/{tx_id:int}/attachment", response_class=HTMLResponse)
def assign_attachment(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    tx_id: int,
    filename: Annotated[str, Form()],
) -> Response:
    """Ordnet einer Buchung eine Quittung aus dem Quittungs-Ordner zu.

    Es wird NICHTS kopiert — nur Dateiname und Pfad (falls die Datei im
    konfigurierten Ordner liegt) werden vermerkt. Anschliessend wird der Text
    ausgelesen (Text-Layer zuerst, OCR-Fallback bei nicht lesbaren Dateien).
    """
    tx = db.get(Transaction, tx_id)
    if tx is None:
        return _render_root(request, db, error="Buchung nicht gefunden.", status_code=404)

    filename = filename.strip()
    if not filename:
        # „keine Quittung" gewählt → nichts zuordnen, einfach zurück (kein Fehler).
        return _render_root(request, db, form_mode="edit", edit_tx=tx,
                            info="Keine Quittung zugeordnet.")

    attach_receipt(db, tx, filename)  # legt Zuordnung an + liest Text aus
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx)


@router.post("/attachment/{att_id:int}/ocr", response_class=HTMLResponse)
def reocr_attachment(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    att_id: int,
) -> Response:
    """Liest den Text einer Quittung erneut aus (z.B. nach OCR-Nachinstallation)."""
    att = db.get(Attachment, att_id)
    if att is None:
        return _render_root(request, db, form_mode="none")
    tx = db.get(Transaction, att.transaction_id)
    _run_ocr(att)
    db.add(att)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx)


@router.get("/attachment/{att_id:int}/ocr-text", response_class=PlainTextResponse)
def attachment_ocr_text(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    att_id: int,
) -> Response:
    """OCR-Text eines Anhangs **on demand** (fürs Beleg-Popup in der Liste) — statt
    ~1.5 KB pro Beleg als data-Attribut in jede Listen-Antwort zu packen."""
    att = db.get(Attachment, att_id)
    if att is None:
        return PlainTextResponse("", status_code=404)
    return PlainTextResponse((att.ocr_text or "")[:1500])


@router.get("/attachment/{att_id:int}")
def serve_attachment(
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    att_id: int,
) -> Response:
    """Liefert die referenzierte Quittungs-Datei aus dem Ordner (inline).

    Nur erlaubt, wenn die Datei nachweislich im konfigurierten Quittungs-Ordner
    liegt (Pfad-Validierung gegen Traversal).
    """
    att = db.get(Attachment, att_id)
    if att is None or not att.file_path:
        return Response(status_code=404)
    resolved = resolve_receipt(att.file_path)
    if resolved is None:
        return Response(status_code=404)
    return FileResponse(
        str(resolved),
        filename=att.original_name or resolved.name,
        content_disposition_type="inline",
    )


@router.post("/attachment/{att_id:int}/delete", response_class=HTMLResponse)
def delete_attachment(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    att_id: int,
) -> Response:
    """Entfernt die Zuordnung (nur DB-Record). Die Original-Datei bleibt im Ordner."""
    att = db.get(Attachment, att_id)
    if att is None:
        return _render_root(request, db, form_mode="none")
    tx = db.get(Transaction, att.transaction_id)
    db.delete(att)
    db.commit()
    return _render_root(request, db, form_mode="edit", edit_tx=tx)
