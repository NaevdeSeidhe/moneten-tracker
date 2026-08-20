"""Bank-Import: CAMT.053-Datei hochladen, Buchungen erzeugen, Saldo abgleichen.

Workflow (Phase 1):
1. Nutzer wählt Zielkonto + lädt die CAMT.053-Datei aus dem E-Banking hoch.
2. Parser liest Buchungen + Schluss-Saldo.
3. Deduplizierung über die Bank-Referenz, sonst über ``dedup_hash`` — Vorhandenes wird
   übersprungen (erneuter Import desselben Zeitraums ist gefahrlos).
4. Buchungen werden angelegt, der Konto-Saldo neu berechnet.
5. **Saldo-Abgleich:** der von der App berechnete Saldo wird mit dem in der
   Datei gemeldeten Schluss-Saldo verglichen. Bei Differenz: Warn-Banner.

Ein Import wird als ``ImportBatch`` protokolliert und kann rückgängig gemacht
werden (löscht genau die Buchungen dieses Batches).
"""

from __future__ import annotations

import html
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import (
    Account,
    Attachment,
    Category,
    ImportBatch,
    ImportSource,
    ImportStatus,
    ManagementType,
    Transaction,
    TransactionSplit,
    User,
    enthaelt,
)
from moneten.db.session import get_db
from moneten.services import anhang_tresor, scan_protokoll
from moneten.services.attachments import list_receipts, receipts_dir, resolve_receipt
from moneten.services.balances import recalc_account_balance
from moneten.services.camt053_parser import make_dedup_hash, parse_camt053_all
from moneten.services.categorization import load_active_rules, match_category, transfer_category_ids
from moneten.services.csv_parser import parse_csv_statements
from moneten.services.jobs import (
    MAX_OFFENE_SCANS,
    get_job,
    job_percent,
    offene_scans,
    scan_job,
    start_match_job,
    start_scan_job,
    step_match_job,
)
from moneten.services.receipt_digital import analyze, match_pending, save_receipt
from moneten.services.receipt_match import (
    archivable_unmatched,
    archive_receipt,
    archived_receipts,
    attach_receipt,
    auto_archive_old,
    build_suggestion_for,
    build_suggestions,
    earliest_transaction_date,
    unarchive_all,
    unarchive_receipt,
    unassigned_log,
    unassigned_receipts,
)
from moneten.services.receipt_ocr import diagnose_receipt_file
from moneten.services.statement_check import pruefe_auszug
from moneten.templating import templates

router = APIRouter(tags=["import"])

# Toleranz beim Saldo-Abgleich (Rundung).
_SALDO_TOLERANZ = Decimal("0.01")

# Maximale Upload-Grösse (CAMT.053-Dateien sind klein; schützt vor Speicher-DoS).
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _active_accounts(db: Session) -> list[Account]:
    return list(
        db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.sort_order, Account.id))
    )


def _recent_batches(db: Session) -> list[ImportBatch]:
    """Die letzten Importe — samt dem, was am Rückgängigmachen mit dranhängt.

    ``Rückgängig`` löscht die Buchungen des Imports, und mit ihnen per Kaskade
    deren Aufteilungen und Beleg-Zuordnungen. Die Abfrage nannte nur die
    Buchungen; von Hand aufgeteilte Beträge und zugeordnete Quittungen
    verschwanden stillschweigend mit, samt dem gelesenen Beleginhalt, aus dem der
    Preisverlauf lebt. Ein zweiter Import bringt sie nicht zurück.

    Zwei gruppierte Abfragen statt zwei pro Zeile — bei 20 Importen sonst 40.
    """
    batches = list(db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(20)))
    if not batches:
        return []
    ids = [b.id for b in batches]

    def _zaehlen(modell) -> dict[int, int]:
        return dict(db.execute(
            select(Transaction.import_batch_id, func.count(modell.id))
            .join(modell, modell.transaction_id == Transaction.id)
            .where(Transaction.import_batch_id.in_(ids))
            .group_by(Transaction.import_batch_id)
        ).all())

    aufteilungen, anhaenge = _zaehlen(TransactionSplit), _zaehlen(Attachment)
    for b in batches:
        # Nur fürs Template, nicht gemappt: reine Python-Attribute am Objekt.
        b.aufteilungen_n = aufteilungen.get(b.id, 0)
        b.anhaenge_n = anhaenge.get(b.id, 0)
    return batches


def _render_page(
    request: Request,
    user: User,
    db: Session,
    *,
    report: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "user": user,
            "active_tab": "import_bank",
            "accounts": _active_accounts(db),
            "batches": _recent_batches(db),
            "account_by_id": {a.id: a for a in db.scalars(select(Account))},
            "report": report,
            "error": error,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Seite
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def import_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Import-Seite: Upload-Formular + Historie vergangener Importe."""
    return _render_page(request, user, db)


# ---------------------------------------------------------------------------
# Import durchführen
# ---------------------------------------------------------------------------


def _norm_iban(value: str | None) -> str:
    """IBAN für den Abgleich normalisieren (ohne Leerzeichen, Grossbuchstaben)."""
    return (value or "").replace(" ", "").upper()


def ist_xml(roh: bytes) -> bool:
    """Ist das eine CAMT.053-Datei (XML) oder eine CSV?

    **Das BOM muss vor der Entscheidung weg.** ``bytes.lstrip()`` entfernt nur
    Leerraum; ein UTF-8-BOM bleibt stehen, und dann ist das erste Byte nicht
    ``<``. E-Banking-Exporte liefern camt.053 durchaus mit BOM — die Datei
    landete im CSV-Zweig und wurde mit einer Begründung abgewiesen, die mit der
    Ursache nichts zu tun hatte.

    Steht als eigene Funktion da, damit die Entscheidung prüfbar ist, ohne den
    halben Importweg zu gehen.
    """
    return roh.lstrip(b"\xef\xbb\xbf").lstrip()[:1] == b"<"


@router.post("", response_class=HTMLResponse)
def run_import(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    account_id: Annotated[int | None, Form()] = None,
    adopt_opening: Annotated[str, Form()] = "",
) -> Response:
    """Verarbeitet **eine oder mehrere** CAMT.053-/CSV-Dateien (z.B. eine Datei pro
    Konto). Jeder Auszug (``<Stmt>``) wird über seine **IBAN automatisch dem
    passenden Konto** zugeordnet; ein manuell gewähltes Zielkonto dient nur als
    **Fallback** für Auszüge ohne IBAN-Treffer.

    Mehrere Dateien: Feld ``files`` (Website-Mehrfachauswahl). Das alte Einzelfeld
    ``file`` bleibt für Kompatibilität erhalten (Deploy-Script, ältere Clients).
    """
    uploads = [u for u in (files or []) if u is not None and u.filename]
    if file is not None and file.filename:
        uploads.append(file)
    if not uploads:
        return _render_page(request, user, db, error="Keine Datei ausgewählt.", status_code=400)

    # IBAN → Konto-Karte (nur Konten mit hinterlegter IBAN) — einmal für alle Dateien.
    by_iban = {_norm_iban(a.iban): a for a in db.scalars(select(Account)) if a.iban}
    fallback = db.get(Account, account_id) if account_id else None
    rule_pairs = load_active_rules(db)  # für Auto-Kategorisierung beim Import
    transfer_ids = transfer_category_ids(db)  # Kategorien, die als Transfer gelten (z.B. Bargeldbezug)

    results: list[dict] = []
    unmatched: list[str] = []
    file_errors: list[str] = []
    total_imported = total_skipped = total_statements = files_ok = 0
    # Welche Konten in DIESEM Aufruf schon einen Startsaldo bekamen, und was
    # dabei passiert ist — der Bericht muss es sagen können.
    startsaldo_gesetzt: set[int] = set()
    startsaldo_hinweise: list[dict] = []

    for up in uploads:
        # Ein Byte über die Grenze hinaus lesen genügt, um „zu gross" zu
        # erkennen. Vorher lag die ganze Datei im Speicher, BEVOR die Zeile
        # darunter sie ablehnte — gemessen 209 MB für ein versehentlich
        # gewähltes Video, gegen ein Container-Limit von 1 GB. Muster aus
        # ``routers/metrics.py``.
        raw = up.file.read(_MAX_UPLOAD_BYTES + 1)
        if not raw:
            file_errors.append(f"{up.filename}: Datei ist leer")
            continue
        if len(raw) > _MAX_UPLOAD_BYTES:
            file_errors.append(f"{up.filename}: zu gross (max. 15 MB)")
            continue
        # Dateityp anhand des Inhalts erkennen: XML (CAMT.053) beginnt mit '<',
        # alles andere wird als CSV-Fallback behandelt.
        #
        # **Das BOM muss vorher weg.** ``bytes.lstrip()`` entfernt nur
        # Leerraum; ein UTF-8-BOM (EF BB BF) bleibt stehen, das erste Byte ist
        # dann nicht ``<``. E-Banking-Exporte liefern camt.053 durchaus mit BOM
        # — die Datei landete beim CSV-Zweig und wurde mit einer Begründung
        # abgewiesen, die nichts mit der Ursache zu tun hatte.
        is_xml = ist_xml(raw)
        try:
            statements = parse_camt053_all(raw) if is_xml else parse_csv_statements(raw)
        except ValueError as exc:
            file_errors.append(f"{up.filename}: Datei konnte nicht gelesen werden ({exc})")
            continue

        files_ok += 1
        total_statements += len(statements)

        for st in statements:
            acc = by_iban.get(_norm_iban(st.iban)) if st.iban else None
            matched_by = "iban" if acc is not None else None
            if acc is None and fallback is not None:
                acc = fallback
                matched_by = "fallback"
            if acc is None:
                unmatched.append(st.iban or "(ohne IBAN)")
                continue

            if adopt_opening and st.opening_balance is not None:
                # **Ein Startsaldo je Konto, und die Änderung wird gemeldet.**
                # Vorher setzte JEDER Auszug ihn neu: bei zwei Dateien in einem
                # Zug gewann die zuletzt gelesene, und bei einem Import auf ein
                # bereits gepflegtes Konto verschwand der von Hand gesetzte Wert
                # ohne ein Wort. Der Kontostand ist ``Startsaldo + Summe aller
                # Buchungen`` — ein stiller Sprung im Startsaldo verschiebt also
                # den ganzen Kontostand, und niemand weiss, woher er kommt.
                if acc.id in startsaldo_gesetzt:
                    startsaldo_hinweise.append({
                        "konto": acc.name, "uebersprungen": True,
                        "alt": None, "neu": st.opening_balance, "vorher_n": 0,
                    })
                else:
                    # Buchungen VOR dem Auszugsbeginn machen die Übernahme falsch:
                    # sie werden zum Startsaldo hinzugezählt, obwohl der
                    # Anfangssaldo des Auszugs sie schon enthält.
                    vorher_n = (db.scalar(select(func.count(Transaction.id)).where(
                        Transaction.account_id == acc.id, Transaction.date < st.period_from,
                    )) or 0) if st.period_from is not None else 0
                    startsaldo_hinweise.append({
                        "konto": acc.name, "uebersprungen": False,
                        "alt": acc.opening_balance, "neu": st.opening_balance,
                        "vorher_n": vorher_n,
                    })
                    startsaldo_gesetzt.add(acc.id)
                    acc.opening_balance = st.opening_balance
                    db.add(acc)

            batch = ImportBatch(
                source=ImportSource.CAMT053,
                filename=up.filename,
                account_id=acc.id,
                period_from=st.period_from,
                period_to=st.period_to,
                expected_closing_balance=st.closing_balance,
                imported_at=datetime.now(UTC),
                status=ImportStatus.PENDING,
            )
            db.add(batch)
            db.flush()

            imported = skipped = 0
            # **Zwei Schlüssel, weil es zwei Sorten Bestand gibt.**
            #
            # Die Bank-Referenz (``AcctSvcrRef``) ist der verlässliche Schlüssel:
            # sie unterscheidet zwei gleich aussehende Buchungen am selben Tag —
            # zweimal derselbe Betrag im selben Laden sind zwei Vorgänge, und ein
            # Inhalts-Hash kann das grundsätzlich nicht auseinanderhalten. Der
            # Hash übersah zusätzlich alles, was sich erst ab Zeichen 51 des
            # Buchungstexts unterscheidet.
            #
            # Der Inhalts-Hash bleibt trotzdem nötig, für zwei Fälle: Buchungen
            # aus der Zeit VOR Migration 0030 (die haben keine Referenz) und
            # CSV-Dateien (die tragen keine). Deshalb wird bei einer Zeile MIT
            # Referenz zusätzlich gegen die Hashes referenzloser Buchungen
            # geprüft — sonst käme ein schon importierter Auszug beim nächsten
            # Import komplett doppelt herein.
            #
            # Alle Mengen EINMAL laden statt einer Abfrage pro Zeile; neu
            # Aufgenommenes wandert mit hinein, damit auch Dubletten innerhalb
            # derselben Datei auffallen.
            # ``konto_id`` als eigene Variable: die Bedingung steckt in einer
            # Hilfsfunktion, und eine Schleifenvariable darin einzufangen ist die
            # Sorte Bindungsfehler, die erst beim Umbauen zuschlägt.
            konto_id = acc.id

            def _hashes(*bedingungen, _kid: int = konto_id) -> set[str]:
                return set(db.scalars(select(Transaction.dedup_hash).where(
                    Transaction.account_id == _kid,
                    Transaction.dedup_hash.is_not(None), *bedingungen,
                )))

            referenzen = set(db.scalars(select(Transaction.bank_reference).where(
                Transaction.account_id == konto_id, Transaction.bank_reference.is_not(None),
            )))
            alle_hashes = _hashes()
            hashes_ohne_referenz = _hashes(Transaction.bank_reference.is_(None))

            for entry in st.entries:
                h = make_dedup_hash(entry.date, entry.amount, entry.description)
                ref = (entry.reference or "").strip() or None
                if ref is not None:
                    if ref in referenzen or h in hashes_ohne_referenz:
                        # Der zweite Fall ist Altbestand: dieselbe Buchung wurde
                        # schon einmal ohne Referenz importiert. Beim doppelten
                        # Vorkommen im Auszug bleibt es dann bei einer Buchung —
                        # so wie es die alte Erkennung hinterlassen hat. Neu ist,
                        # dass es ab jetzt nicht mehr passiert.
                        skipped += 1
                        continue
                    referenzen.add(ref)
                    alle_hashes.add(h)
                else:
                    if h in alle_hashes:
                        skipped += 1
                        continue
                    alle_hashes.add(h)
                    hashes_ohne_referenz.add(h)
                desc = entry.description or (entry.reference or "Bank-Buchung")
                cat = match_category(rule_pairs, desc)
                db.add(Transaction(
                    account_id=acc.id, category_id=cat, date=entry.date, amount=entry.amount,
                    description=desc, dedup_hash=h, bank_reference=ref, import_batch_id=batch.id,
                    management_type=(ManagementType.TRANSFER if cat in transfer_ids else None),
                ))
                imported += 1

            db.flush()
            recalc_account_balance(db, acc.id)
            db.flush()

            actual = acc.current_balance
            expected = st.closing_balance
            match = abs(actual - expected) <= _SALDO_TOLERANZ if expected is not None else None
            batch.total_transactions = imported
            batch.actual_closing_balance = actual
            batch.balance_match = match
            batch.status = ImportStatus.COMPLETED
            db.add(batch)

            total_imported += imported
            total_skipped += skipped
            results.append({
                "filename": up.filename,
                "account": acc, "iban": st.iban, "matched_by": matched_by,
                "imported": imported, "skipped": skipped, "total_in_file": len(st.entries),
                "period_from": st.period_from, "period_to": st.period_to,
                "expected_closing": expected, "actual_closing": actual, "balance_match": match,
                "balance_diff": (actual - expected) if expected is not None else None,
                # Zweite, unabhaengige Pruefung: geht die Datei in sich auf?
                # Der Saldo-Vergleich oben kann auch an fehlenden Altdaten liegen;
                # diese hier faellt nur aus, wenn beim Lesen Buchungen verloren
                # gingen — ein Fehler VOR dem Import.
                "auszug": pruefe_auszug(st) if is_xml else None,
                # Nur bei CSV gefüllt: Zeilen, die der Leser nicht verwerten konnte.
                "weg_zeilen": st.uebersprungene_zeilen,
                "weg_beispiele": st.uebersprungene_beispiele,
            })

    db.commit()

    # Quittungs-Abgleich NICHT mehr blockierend hier — der Report bettet ein
    # Fortschritts-Widget ein, das den Abgleich im Hintergrund (per Polling)
    # erledigt. So bleibt der Import sofort fertig, auch mit vielen Belegen.
    report = {
        "files_count": files_ok,
        "statements": total_statements,
        "results": results,
        "unmatched": unmatched,
        "file_errors": file_errors,
        "total_imported": total_imported,
        "total_skipped": total_skipped,
        "match_after_import": receipts_dir() is not None,
        "startsaldo": startsaldo_hinweise,
    }
    status = 400 if (not results and (file_errors or unmatched)) else 200
    return _render_page(request, user, db, report=report, status_code=status)


# ---------------------------------------------------------------------------
# Import rückgängig machen
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Quittungs-Zuordnungs-Assistent
# ---------------------------------------------------------------------------


def _receipt_options(db: Session, s, attached: set[int]) -> list[Transaction]:
    """Optionen fürs manuelle Zuordnen: Kandidaten zuerst, dann weitere Buchungen ohne
    Anhang IM ±120-Tage-Fenster des Belegs — so findet man auch ältere Überweisungen,
    nicht nur die neuesten."""
    cand_ids = {t.id for t in s.candidates}
    q = select(Transaction).order_by(Transaction.date.desc(), Transaction.id.desc())
    if s.date_guess is not None:
        q = (
            select(Transaction)
            .where(Transaction.date.between(
                s.date_guess - timedelta(days=120), s.date_guess + timedelta(days=120)))
            .order_by(Transaction.date.desc(), Transaction.id.desc())
        )
    near = [t for t in db.scalars(q.limit(250)) if t.id not in attached and t.id not in cand_ids]
    return list(s.candidates) + near


@router.get("/receipts", response_class=HTMLResponse)
def receipts_assistant(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Zeigt unzugeordnete Quittungen aus dem Ordner mit Buchungs-Vorschlägen."""
    has_folder = receipts_dir() is not None
    # Vorgemerkte Foto-Belege nachträglich zuordnen (falls inzwischen die passende
    # Bankbuchung importiert wurde).
    match_pending(db)
    # Belege vor dem ersten Bank-Buchungstag automatisch ablegen (kein Bankeintrag
    # zum Zuordnen) — der Nutzer muss diese alten Quittungen nicht selbst abarbeiten.
    auto_archived = auto_archive_old(db) if has_folder else 0
    cutoff = earliest_transaction_date(db) if has_folder else None
    # Liste SCHNELL laden: nur die ersten N, OHNE teures OCR (sonst läuft die Seite bei
    # vielen Foto-Belegen in einen Timeout). Den Rest erledigt der schrittweise
    # Hintergrund-Abgleich („⚡ automatisch zuordnen"), der pro Beleg ein OCR macht.
    total_unassigned = len(unassigned_receipts(db)) if has_folder else 0
    suggestions = build_suggestions(db, limit=50, ocr=False) if has_folder else []

    attached = set(db.scalars(select(Attachment.transaction_id)))
    rows = [{"s": s, "options": _receipt_options(db, s, attached)} for s in suggestions]

    return templates.TemplateResponse(
        request,
        "receipts_match.html",
        {
            "user": user,
            "active_tab": "import_bank",
            "has_folder": has_folder,
            "rows": rows,
            "total_unassigned": total_unassigned,
            "auto_archived": auto_archived,
            "cutoff": cutoff,
        },
    )


@router.post("/receipts/assign")
def receipts_assign(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    filename: Annotated[str, Form()],
    transaction_id: Annotated[int, Form()],
) -> Response:
    """Bestätigt eine (ggf. korrigierte) Zuordnung Quittung → Buchung.

    Per HTMX wird die Beleg-Karte durch eine **grüne Bestätigung** ersetzt
    (klares Feedback, kein Voll-Reload); klassischer POST → Redirect (Fallback).
    """
    tx = db.get(Transaction, transaction_id)
    att_id = None
    if tx is not None and filename.strip():
        att = attach_receipt(db, tx, filename.strip())
        db.commit()
        att_id = att.id
    if request.headers.get("HX-Request") == "true":
        if att_id is None:
            # KEIN Erfolgsbanner, wenn nichts zugeordnet wurde (Buchung existiert
            # nicht mehr, z. B. Import rückgängig gemacht). 4xx-HTML swappt der
            # htmx:beforeSwap-Handler in app.js inline ein.
            return HTMLResponse(
                '<div class="card" style="margin-bottom:16px">'
                '<div class="banner-ok" style="border-left-color:var(--danger,#e5484d)">'
                '✗ Zuordnung fehlgeschlagen — die Buchung existiert nicht mehr. '
                'Bitte Seite neu laden.</div></div>',
                status_code=409,
            )
        return templates.TemplateResponse(
            request, "partials/receipt_assigned.html",
            {"filename": filename.strip(), "tx": tx, "att_id": att_id},
        )
    return RedirectResponse(url="/import/receipts", status_code=303)


@router.get("/receipts/tx-options", response_class=HTMLResponse)
def receipts_tx_options(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
) -> Response:
    """Server-Suche fürs Zuordnen-Dropdown: liefert <option>s passender, noch NICHT
    zugeordneter Buchungen über ALLE Buchungen (beliebiges Datum). So findet man auch
    per Überweisung bezahlte Rechnungen (Zahlung Wochen nach Rechnungsdatum) und kann
    sie VERKNÜPFEN, statt sie archivieren zu müssen. Jeder Sucheteil (durch Leerzeichen
    getrennt) muss vorkommen — Betrag (Komma wird zu Punkt), Text und Datum kombinierbar."""
    parts = [p.replace(",", ".") for p in q.strip().lower().split() if p]
    # SQL-Vorfilterung (Obermenge) statt Python-Full-Scan über ALLE Buchungen: je
    # Suchteil muss Beschreibung ODER Datum (dd.mm.yyyy) ODER Betrag (2 Stellen,
    # mit/ohne Vorzeichen) passen. Die exakte Prüfung läuft danach wie bisher in
    # Python — nur noch über die (wenigen) vorgefilterten Zeilen.
    stmt = (
        select(Transaction)
        .where(Transaction.id.not_in(
            select(Attachment.transaction_id).where(Attachment.transaction_id.is_not(None))
        ))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    for p in parts:
        # enthaelt() maskiert die LIKE-Platzhalter: ein „%" im Suchfeld traf
        # sonst jede noch nicht zugeordnete Buchung — und die ersten 400 davon
        # standen als Vorschlaege im Dropdown.
        stmt = stmt.where(or_(
            enthaelt(Transaction.description, p),
            enthaelt(func.printf("%.2f", func.abs(Transaction.amount)), p),
            enthaelt(func.printf("%.2f", Transaction.amount), p),
            enthaelt(func.strftime("%d.%m.%Y", Transaction.date), p),
        ))
    txs: list[Transaction] = []
    for t in db.scalars(stmt.limit(400)):
        hay = (
            f"{t.date.strftime('%d.%m.%Y')} {t.description or ''} "
            f"{abs(t.amount):.2f} {t.amount}"
        ).lower()
        if all(p in hay for p in parts):
            txs.append(t)
            if len(txs) >= 80:
                break
    return templates.TemplateResponse(request, "partials/tx_options.html", {"txs": txs})


@router.post("/receipts/unassign", response_class=HTMLResponse)
def receipts_unassign(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    att_id: Annotated[int, Form()],
) -> Response:
    """Macht eine Beleg-Zuordnung rückgängig (löscht den Anhang) und rendert die Karte
    wieder als Zuordnen-Form — für „falsch zugeordnet"."""
    att = db.get(Attachment, att_id)
    filename = att.original_name if att is not None else None
    if att is not None:
        db.delete(att)
        db.commit()
    s = build_suggestion_for(db, filename) if filename else None
    if s is None:
        return HTMLResponse('<div class="card"><div class="banner-ok">↩ Zuordnung aufgehoben.</div></div>')
    attached = set(db.scalars(select(Attachment.transaction_id)))
    return templates.TemplateResponse(
        request, "partials/receipt_card.html",
        {"s": s, "options": _receipt_options(db, s, attached), "idx": att_id},
    )


@router.post("/receipts/archive")
def receipts_archive(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    filename: Annotated[str, Form()],
) -> Response:
    """Legt eine Quittung ohne Bankeintrag ab (z.B. wenn es keine passende Buchung
    gibt). Sie verschwindet aus dem Assistenten; die Datei bleibt im Ordner."""
    archive_receipt(db, filename.strip(), reason="manuell")
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "partials/receipt_archived.html", {"filename": filename.strip()}
        )
    return RedirectResponse(url="/import/receipts", status_code=303)


@router.get("/receipts/file")
def serve_receipt_file(
    user: Annotated[User, Depends(require_login)],
    name: str,
) -> Response:
    """Liefert eine (noch nicht zugeordnete) Beleg-Datei aus dem Ordner **inline** —
    zum Ansehen im Zuordnen-Assistenten. Pfad-validiert gegen Traversal: nur Dateien,
    die wirklich im konfigurierten Quittungs-Ordner liegen."""
    match = next((r for r in list_receipts() if r.name == name), None)
    if match is None:
        return Response(status_code=404)
    resolved = resolve_receipt(match.path)
    if resolved is None:
        return Response(status_code=404)
    return FileResponse(str(resolved), filename=match.name, content_disposition_type="inline")


@router.post("/receipts/diagnose", response_class=HTMLResponse)
def receipts_diagnose(
    user: Annotated[User, Depends(require_login)],
    filename: Annotated[str, Form()],
) -> Response:
    """OCR-Diagnose für EINEN Beleg (warum kein Text erkannt wird) — auf Knopfdruck,
    weil OCR teuer ist. Liefert den Diagnose-Block als ``<pre>`` zum Einswappen."""
    match = next((r for r in list_receipts() if r.name == filename), None)
    text = diagnose_receipt_file(match.path) if match else "[OCR-Diagnose] Datei nicht gefunden."
    return HTMLResponse(
        f'<pre class="receipt-preview" style="white-space:pre-wrap;margin-top:8px">{html.escape(text)}</pre>'
    )


@router.post("/receipts/archive-unmatchable", response_class=HTMLResponse)
def receipts_archive_unmatchable(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Archiviert in EINEM Rutsch alle Belege ohne mögliche Bankbuchung (Rechnung/
    Bargeld) bzw. Duplikate. Die Dateien bleiben im Ordner."""
    names = archivable_unmatched(db) if receipts_dir() is not None else []
    for name in names:
        archive_receipt(db, name, reason="ohne-bankbuchung")
    return HTMLResponse(
        f'<div class="banner-ok">🗄 {len(names)} Beleg(e) archiviert (Rechnung/Bargeld/Duplikat). '
        f'Die Dateien bleiben im Ordner. <a href="/import/receipts">Liste neu laden</a></div>'
    )


@router.get("/receipts/archived", response_class=HTMLResponse)
def receipts_archived(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Übersicht aller archivierten Belege (mit Grund) und Knöpfen zum Reaktivieren —
    damit nichts „still" verschwindet und falsch (z. B. mit altem Total) Archiviertes
    zurückgeholt werden kann."""
    rows = archived_receipts(db) if receipts_dir() is not None else []
    return templates.TemplateResponse(
        request, "receipts_archived.html",
        {"user": user, "active_tab": "import_bank", "rows": rows},
    )


@router.post("/receipts/unarchive", response_class=HTMLResponse)
def receipts_unarchive(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    filename: Annotated[str, Form()],
) -> Response:
    """Reaktiviert EINEN archivierten Beleg → er erscheint wieder im Assistenten."""
    ok = unarchive_receipt(db, filename)
    if ok:
        return HTMLResponse('<span class="muted text-sm">↩ reaktiviert — erscheint wieder im Assistenten.</span>')
    return HTMLResponse('<span class="muted text-sm">— nicht gefunden —</span>')


@router.post("/receipts/unarchive-all", response_class=HTMLResponse)
def receipts_unarchive_all(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    reason: Annotated[str, Form()] = "",
) -> Response:
    """Reaktiviert ALLE archivierten Belege (oder nur die mit einem ``reason``, z. B.
    „ohne-bankbuchung" nach einer Total-Korrektur)."""
    n = unarchive_all(db, reason=reason or None) if receipts_dir() is not None else 0
    return HTMLResponse(
        f'<div class="banner-ok">↩ {n} Beleg(e) reaktiviert. '
        f'<a href="/import/receipts">Zum Assistenten</a> und „⚡ automatisch zuordnen" erneut laufen lassen.</div>'
    )


@router.get("/receipts/log", response_class=HTMLResponse)
def receipts_log(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Kopierbarer Diagnose-Log ALLER unzugeordneten Belege (Dateiname, Betrag, Datum,
    Quelle, Grund) — zum Teilen, damit gezielt nachgebessert werden kann."""
    rows = unassigned_log(db) if receipts_dir() is not None else []
    return templates.TemplateResponse(
        request, "receipts_log.html",
        {"user": user, "active_tab": "import_bank", "rows": rows},
    )


def _progress(request: Request, jid: str, job: dict | None) -> Response:
    """Rendert das Fortschritts-Widget (runder Ring) für einen Abgleich-Job."""
    if job is None:  # unbekannt/abgelaufen → als fertig anzeigen, kein weiteres Polling
        job = {"done": True, "total": 0, "idx": 0, "matched": 0, "current": ""}
    return templates.TemplateResponse(
        request, "partials/match_progress.html",
        {"jid": jid, "job": job, "percent": job_percent(job)},
    )


@router.post("/receipts/auto/begin", response_class=HTMLResponse)
def receipts_auto_begin(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Startet den schrittweisen Auto-Abgleich (Bulk / nach Import) und liefert
    das Fortschritts-Widget, das sich danach selbst per Polling weitertreibt."""
    jid = start_match_job(db)
    return _progress(request, jid, get_job(jid))


@router.get("/receipts/auto/step", response_class=HTMLResponse)
def receipts_auto_step(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    job: str,
) -> Response:
    """Verarbeitet die nächste Quittung des Jobs und liefert den aktuellen Stand."""
    return _progress(request, job, step_match_job(db, job))


def _flat_categories(db: Session) -> list[dict]:
    """Flache Kategorie-Liste (Unterkategorien) für den Positions-Picker."""
    tops = {c.id: c for c in db.scalars(select(Category).where(Category.parent_id.is_(None)))}
    out: list[dict] = []
    for c in db.scalars(
        select(Category)
        .where(Category.parent_id.is_not(None), Category.is_archived.is_(False))
        .order_by(Category.sort_order)
    ):
        top = tops.get(c.parent_id)
        out.append({"id": c.id, "name": c.name, "group": top.name if top else "", "icon": c.icon})
    return out


def _foto_ordner() -> Path:
    """Der EINZIGE Ort, an dem behaltene Beleg-Fotos liegen dürfen.

    Liegt im Tresor-Modul, weil auch das Aufräumen (``anhang_tresor.entfernen``)
    denselben Ordner kennen muss — zwei Stellen mit demselben Pfad laufen
    irgendwann auseinander, und dann löscht die eine, wo die andere schreibt.
    """
    return anhang_tresor.foto_ordner()


def _geprueftes_fotoziel(pfad: str) -> str | None:
    """Nimmt einen Bildpfad nur an, wenn er wirklich in diesem Ordner liegt.

    Der Pfad kommt aus einem Formularfeld zurück, also vom Browser — und was von
    dort kommt, ist ein Vorschlag, keine Tatsache. Heute wird er nur gespeichert
    und nirgends ausgeliefert; gerade deshalb steht die Prüfung dort, wo der Wert
    HEREINKOMMT. An dem Tag, an dem jemand das Bild anzeigen will, ist niemand
    mehr da, der weiss, dass dieser Pfad ungeprüft in der Datenbank steht.
    """
    if not pfad:
        return None
    try:
        ziel = Path(pfad).resolve()
        ziel.relative_to(_foto_ordner().resolve())
    except (ValueError, OSError):
        return None
    return str(ziel) if ziel.is_file() else None


def _save_reduced_photo(data: bytes) -> str | None:
    """Speichert ein reduziertes Graustufen-Bild (Safety) und gibt den Pfad zurück."""
    try:
        from PIL import ImageOps

        from moneten.services.receipt_ocr import pil_image
        Image = pil_image()   # mit Bildbomben-Grenze
        base = _foto_ordner()
        base.mkdir(parents=True, exist_ok=True)
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
        w, h = img.size
        if max(w, h) > 1200:
            s = 1200 / max(w, h)
            img = img.resize((int(w * s), int(h * s)))
        name = f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}.jpg"

        # **Verschluesselt ablegen, wenn die Datenbank verschluesselt ist.**
        # Frueher lag hier ein gewoehnliches JPEG: ein
        # abfotografierter Kassenzettel mit Haendler, Datum, Summe und jeder
        # Position — dieselben Daten, die nebenan in SQLCipher liegen, offen im
        # Dateisystem. Das Bild geht deshalb ueber ``anhang_tresor.schreiben``,
        # das ohne Schluessel unveraendert Klartext schreibt (Entwicklung,
        # Tests) und mit Schluessel AES-256-GCM.
        puffer = io.BytesIO()
        img.save(puffer, "JPEG", quality=70)
        return str(anhang_tresor.schreiben(base / name, puffer.getvalue()))
    except Exception:
        return None


# Hier stand die alte, synchrone Foto-Route. Ersetzt wurde sie von
# ``/receipts/photo/start`` samt Auftrag und Abholung. Entfernt statt bloss
# unbenutzt liegengelassen: eine registrierte Route, die niemand mehr aufruft,
# umgeht trotzdem jede Schranke, die spaeter dazukommt.


@router.post("/receipts/photo/start")
def receipt_photo_start(
    user: Annotated[User, Depends(require_login)],
    photo: Annotated[UploadFile, File()],
) -> Response:
    """Nimmt das Foto an und gibt SOFORT eine Auftragsnummer zurueck.

    Der alte Weg (``/receipts/photo``) liess den Browser warten, bis die
    Erkennung fertig war. Auf der NAS-CPU sind das Minuten, und wer in der Zeit
    kurz in eine andere App wechselt, kommt zurueck und hat nichts: das Handy
    haelt die Seite im Hintergrund nicht am Leben, die Anfrage stirbt mit ihr,
    und das Ergebnis stand NUR in dieser Antwort.
    """
    # **Zuerst zaehlen, dann lesen.** Jeder wartende Auftrag haelt seine
    # Bilddaten im Speicher fest. Wer die Pruefung erst NACH dem Einlesen macht,
    # hat die 15 MB schon geholt, die er gerade ablehnen will.
    if offene_scans() >= MAX_OFFENE_SCANS:
        return JSONResponse(
            {"fehler": "Es laufen gerade genug Erkennungen. Bitte kurz warten."},
            status_code=429,
        )
    data = photo.file.read(_MAX_UPLOAD_BYTES + 1)  # siehe Kommentar beim CAMT-Upload
    if len(data) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"fehler": "Datei zu gross (max. 15 MB)."}, status_code=413)
    suffix = Path(photo.filename or "").suffix.lower()
    jid = start_scan_job(
        data, suffix,
        bild_speichern=_save_reduced_photo if user.receipt_photo_keep else None,
    )
    return JSONResponse({"jid": jid})


@router.get("/receipts/photo/job/{jid}", response_class=HTMLResponse)
def receipt_photo_job(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    jid: str,
) -> Response:
    """Holt das Ergebnis eines Scan-Auftrags ab.

    ``202`` heisst „laeuft noch" — der Browser fragt spaeter wieder. ``410``
    heisst „Nummer unbekannt": der Server wurde neu gestartet oder der Auftrag
    ist aus der Historie gefallen. Beides kann die Oberflaeche nur gleich
    behandeln, darum ein eigener Code statt einer stillen Leerantwort.

    Die AUSWERTUNG passiert hier und nicht im Thread: sie braucht eine
    Datenbank-Sitzung, und die gehoert der Anfrage.
    """
    auftrag = scan_job(jid)
    if auftrag is None:
        return JSONResponse({"fehler": "unbekannt"}, status_code=410)
    if auftrag["zustand"] == "laeuft":
        return JSONResponse({"zustand": "laeuft"}, status_code=202)
    if auftrag["zustand"] == "fehler":
        return templates.TemplateResponse(
            request, "partials/receipt_scan_editor.html",
            {"receipt": None, "categories": [], "error": "Beleg liess sich nicht lesen."},
        )

    ocr = auftrag["ocr"]
    structured = analyze(db, ocr)
    # Festhalten, was die Erkennung gesehen hat. Ohne das war der Rohtext nur im
    # offenen Dialog zu haben — Fenster zu, Text weg — und ein Erkennungsfehler
    # liess sich nur nachstellen, indem man den Beleg abfotografierte.
    scan_protokoll.protokolliere(db, quittung=structured, ocr_text=ocr.text or "",
                                 methode=ocr.method)
    # Frische Foto-Scans: wenn kein Datum aus dem (oft am Rand verwischten) Beleg
    # gelesen wurde, heute vorbelegen.
    if not structured.get("date"):
        structured["date"] = heute_lokal().isoformat()
    return templates.TemplateResponse(
        request, "partials/receipt_scan_editor.html",
        {
            "receipt": structured,
            "categories": _flat_categories(db),
            "ocr_text": ocr.text or "",
            "image_path": auftrag["bild"] or "",
            "no_text": not (ocr.text or "").strip(),
        },
    )


@router.post("/receipts/photo/confirm", response_class=HTMLResponse)
def receipt_photo_confirm(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    data: Annotated[str, Form()] = "",
    ocr_text: Annotated[str, Form()] = "",
    image_path: Annotated[str, Form()] = "",
) -> Response:
    """Speichert die geprüfte/korrigierte digitale Quittung: lernt daraus und ordnet
    sie einer Buchung zu — oder merkt sie vor. Das Foto bleibt verworfen (bzw.
    reduziert behalten je Einstellung)."""
    try:
        structured = json.loads(data) if data else {}
    except (ValueError, TypeError):
        structured = {}
    if not isinstance(structured, dict):
        # Gültiges JSON, das kein Objekt ist (``5``, ``[1,2]``, ``null``): der
        # ``.get``-Aufruf darunter wäre ein AttributeError und damit eine nackte
        # 500-Seite. Die App selbst schickt immer ein Objekt — ein von Hand
        # gebauter Aufruf oder ein künftiger Aufrufer aber nicht.
        structured = {}
    if not structured.get("items") and not structured.get("amount"):
        # 422 statt 200: hier wurde NICHTS gespeichert, und app.js liest genau
        # daran ab, ob es den Entwurf des Belegs wegwerfen darf. Ein 200 hätte
        # die Analyse verworfen, obwohl sie noch gebraucht wird.
        return templates.TemplateResponse(
            request, "partials/receipt_saved.html",
            {"result": None, "error": "Weder Positionen noch Betrag erkannt — nichts gespeichert."},
            status_code=422,
        )
    keep = _geprueftes_fotoziel(image_path) if user.receipt_photo_keep else None
    result = save_receipt(db, structured, ocr_text or None, source="photo", image_path=keep)
    tx = db.get(Transaction, result["attached_tx_id"]) if result.get("attached_tx_id") else None
    return templates.TemplateResponse(
        request, "partials/receipt_saved.html",
        {"result": result, "tx": tx, "merchant": structured.get("merchant") or "Beleg"},
    )


@router.post("/{batch_id:int}/delete")
def delete_batch(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    batch_id: int,
) -> Response:
    """Macht einen Import rückgängig: löscht dessen Buchungen, rechnet Saldo neu.

    Mit den Buchungen gehen per Kaskade auch ihre Aufteilungen und
    Beleg-Zuordnungen (``attachments``) — inklusive OCR-Text und gelesener
    Positionen. Die Beleg-DATEIEN im Quittungs-Ordner bleiben liegen. Wie viel
    daran hängt, steht in der Sicherheitsabfrage; siehe ``_recent_batches``.
    """
    batch = db.get(ImportBatch, batch_id)
    if batch is not None:
        account_id = batch.account_id
        for tx in db.scalars(select(Transaction).where(Transaction.import_batch_id == batch_id)):
            db.delete(tx)
        db.delete(batch)
        db.flush()
        if account_id is not None:
            recalc_account_balance(db, account_id)
        db.commit()
    return RedirectResponse(url="/import", status_code=303)
