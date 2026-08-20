"""Wegwerf-Dev-Server für die visuelle Browser-Prüfung (NICHT deployen).

Startet die App mit einer FRISCHEN Dummy-DB (kein Zugriff auf echte Daten) und
befüllt sie mit realistischen Test-Buchungen über mehrere Monate, damit jede
Seite voll ist und am Handy-Viewport geprüft werden kann.

Aufruf::

    python scripts/_dev_server.py

Danach http://127.0.0.1:8000 im Browser.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# src/ auf den Importpfad, falls das Paket nicht editierbar installiert ist.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# --- Umgebung VOR jedem moneten-Import setzen (config liest beim Import) ----
# Wohin die Wegwerf-Daten kommen. Voreinstellung ist ``_devdata`` neben dem
# Paket; ``MONETEN_DEV_DIR`` verlegt sie. Gebraucht wird das beim Aufnehmen der
# README-Bilder: die laufen gegen den EXPORT, und in einen Export gehört keine
# Datenbank — der Prüfer verbietet den Ordner ``_devdata`` ausdrücklich.
_DEV = Path(os.environ.get("MONETEN_DEV_DIR") or (Path(__file__).resolve().parent.parent / "_devdata"))
_DEV.mkdir(parents=True, exist_ok=True)
os.environ["MONETEN_DATABASE_URL"] = f"sqlite:///{(_DEV / 'dev.db').as_posix()}"
os.environ["MONETEN_ATTACHMENTS_DIR"] = str(_DEV / "attachments")
os.environ["MONETEN_SECRET_KEY"] = "dev-only-secret-key-not-for-production-use-1234567890"
os.environ["MONETEN_INITIAL_PIN"] = "123456"
os.environ["MONETEN_DEV_MODE"] = "true"  # Cookie ohne Secure-Flag → http localhost ok

from sqlalchemy import select  # noqa: E402

from moneten.db.models import (  # noqa: E402
    Account,
    AccountType,
    Attachment,
    Base,
    Budget,
    BudgetInterval,
    Category,
    ManagementType,
    ManualSubscription,
    MeetContribution,
    MeetFundSettings,
    MeetVisit,
    SavingsGoal,
    StandardBudget,
    Transaction,
    TransactionSplit,
)
from moneten.db.seeds import seed_all  # noqa: E402
from moneten.db.session import SessionLocal, engine  # noqa: E402


def _month_starts(n: int) -> list[date]:
    """Liefert die ersten Tage der letzten ``n`` Monate bis und MIT dem aktuellen.

    Anker ist ``date.today()`` — so ist der laufende Monat (Dashboard „diesen
    Monat") immer befüllt, egal an welchem Datum der Dev-Server läuft.
    """
    base = date.today().replace(day=1)
    out = []
    y, m = base.year, base.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def _dom(ms: date, day: int) -> date:
    """Tag im Monat ``ms``; im laufenden Monat auf ``<= heute`` begrenzt."""
    today = date.today()
    if ms.year == today.year and ms.month == today.month:
        day = min(day, today.day)
    return date(ms.year, ms.month, day)


def _seed_dummy() -> None:
    """Frische Dummy-Daten: Buchungen, Splits, Abo, Sparziel, Budgets.

    **Zwei Regeln fuer Platzhalter**, damit Erfundenes als solches zu erkennen ist:

    * Firmen und Produkte heissen „Muster…", „Beispiel…" oder nach ihrer Gattung
      („Elektronik AG", „Grossverteiler").
    * PERSONEN tragen einen Namen aus der Sage — Odysseus, Penelope. Ein
      frei erfundener Vorname ist von einem echten nicht zu unterscheiden —
      genau daran ist eine Pruefung schon einmal vorbeigelaufen.

    Diese Daten landen NIE in einer benutzten Anlage: sie entstehen in einer
    Wegwerf-Datenbank, die der Entwicklungsserver beim Start anlegt.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_all(db)

        # PIN gilt hier als gesetzt. Sonst schickt die Erst-Wechsel-Sperre jede
        # Seite auf /pin-aendern — fuer eine echte Installation richtig, hier
        # sinnlos: die Wegwerf-PIN steht im Kopf dieser Datei, und der Zweck des
        # Servers ist gerade, sich die anderen Seiten anzusehen.
        from datetime import UTC, datetime

        from moneten.db.models import User

        benutzer = db.get(User, 1)
        if benutzer is not None:
            benutzer.pin_changed_at = datetime.now(UTC)

        cats = {c.name: c for c in db.scalars(select(Category)).all()}
        accs = {a.name: a for a in db.scalars(select(Account)).all()}
        priv = accs["Privatkonto"]
        cash = accs["Geldkassette"]
        spar = accs["Sparkonto"]

        priv.opening_balance = Decimal("3000")
        cash.opening_balance = Decimal("200")
        spar.opening_balance = Decimal("12000")

        def tx(acc, cat_name, d, amount, desc, mgmt=None):
            cat = cats.get(cat_name)
            db.add(
                Transaction(
                    account_id=acc.id,
                    category_id=cat.id if cat else None,
                    date=d,
                    amount=Decimal(amount),
                    description=desc,
                    management_type=(mgmt or (cat.management_type if cat else None)),
                )
            )

        months = _month_starts(4)
        for ms in months:
            m = ms.month
            tx(priv, "Nettolohn", _dom(ms, 25), "5500.00", "Lohn Arbeitgeber AG", ManagementType.EINKOMMEN)
            tx(priv, "Miete", _dom(ms, 1), "-1450.00", "Miete Wohnung")
            tx(priv, "Krankenkasse Grund (KVG)", _dom(ms, 1), "-389.40", "Musterkasse Praemie")
            tx(priv, "Internet / TV", _dom(ms, 3), "-69.00", "Musterfunk Home Internet")
            tx(priv, "Handy-Abo", _dom(ms, 5), "-39.90", "Musterfunk Mobile Abo")
            tx(priv, "KI-Dienste", _dom(ms, 8), "-22.00", "Musterdienst KI-Abo")
            tx(priv, "Streaming", _dom(ms, 9), "-24.90", "Streamdienst Abo")
            tx(priv, "Lebensmittel", _dom(ms, 6), "-112.35", "Supermarkt Nord")
            tx(priv, "Lebensmittel", _dom(ms, 14), "-86.20", "Supermarkt Mitte")
            tx(priv, "Lebensmittel", _dom(ms, 21), "-94.75", "Supermarkt Sued")
            tx(priv, "Auswärts essen privat", _dom(ms, 12), "-48.00", "Restaurant Beispiel")
            tx(cash, "Kaffee", _dom(ms, 7), "-4.50", "Kaffee to go")
            tx(cash, "Kaffee", _dom(ms, 17), "-5.00", "Café Pause")
            tx(cash, "Gaming", _dom(ms, 19), "-15.00", "Musterspiel Handyspiel")
            tx(priv, "Coiffeur", _dom(ms, 16), "-45.00", "Coiffeur Termin")
            # Rückzahlung TWINT an Privatperson
            tx(priv, "Rückzahlungen", _dom(ms, 18), "-30.00", "TWINT an Bekannte")
            # Gegenbuchung / Gutschrift (Refund)
            if m % 2 == 0:
                tx(priv, "Lebensmittel", _dom(ms, 22), "12.40", "Supermarkt Rückerstattung")

        db.flush()

        # Referenz-Monat für Split/Transfer/Budget: der letzte abgeschlossene Monat.
        prev_m = months[-2]

        # Eine aufgeteilte Buchung (Auto-Split).
        split_parent = Transaction(
            account_id=priv.id,
            category_id=cats["Lebensmittel"].id,
            date=_dom(prev_m, 24),
            amount=Decimal("-78.40"),
            description="Supermarkt Grosseinkauf",
            management_type=ManagementType.KOST_LOGIS,
            is_split=True,
        )
        db.add(split_parent)
        db.flush()
        db.add_all([
            TransactionSplit(transaction_id=split_parent.id, category_id=cats["Lebensmittel"].id, amount=Decimal("-56.80")),
            TransactionSplit(transaction_id=split_parent.id, category_id=cats["Haushalt"].id, amount=Decimal("-15.20")),
            TransactionSplit(transaction_id=split_parent.id, category_id=cats["Alkohol"].id, amount=Decimal("-6.40")),
        ])
        # Quittung an die Split-Buchung hängen (für Beleg-Popup-Test). Datei nur
        # referenziert (file_path None) — kein echter Datei-Zugriff nötig.
        db.add(Attachment(
            transaction_id=split_parent.id, file_path=None, mime_type="application/pdf",
            original_name="Supermarkt_Bon_2026.pdf",
            ocr_text="SUPERMARKT NORD\nMilch 1.45\nBrot 2.80\nWein 6.40\nWaschmittel 15.20\nTotal 78.40",
        ))

        # Ein paar UNKATEGORISIERTE Buchungen (für Regeln-Inbox + Dashboard-Hinweis).
        for d, amt, desc in [
            (_dom(months[-1], 4), "-42.00", "TWINT QR-Zahlung Markt"),
            (_dom(months[-1], 5), "-23.50", "SumUp *Foodtruck"),
            (_dom(months[-1], 6), "-150.00", "Bezug Bancomat Filiale"),
        ]:
            db.add(Transaction(account_id=priv.id, category_id=None, date=d,
                               amount=Decimal(amt), description=desc))

        # Eine GROSSE unkategorisierte Gruppe (E-Banking-Aufträge) — zum Testen von
        # Gruppen-Suche + Mehrfachauswahl/Bulk in der Inbox. Mehrere enthalten
        # „burger" → Filtern nach „burger" zeigt nur diese.
        _ebank = [
            "E-Banking Auftrag an Zahnarztpraxis AG", "E-Banking Auftrag an Optiker AG",
            "E-Banking Auftrag an Gebuehrenstelle AG", "E-Banking Auftrag an Sozialversicherung",
            "E-Banking Auftrag an Burger Nord", "E-Banking Auftrag an Burger Mitte",
            "E-Banking Auftrag an Burger Sued", "E-Banking Auftrag an Kleinanzeigen AG",
            "E-Banking Auftrag an Elektronik AG", "E-Banking Auftrag an Musterus Versicherung",
            "E-Banking Auftrag an Musterfunk Mobile", "E-Banking Auftrag an Rechtsschutz AG",
        ]
        for i, desc in enumerate(_ebank):
            mm = months[i % len(months)]
            db.add(Transaction(account_id=priv.id, category_id=None,
                               date=_dom(mm, (i % 25) + 1),
                               amount=Decimal(str(-(20 + i * 7))), description=desc))

        # Transfer (Bargeldbezug) — Paar mit transfer_group_id.
        import uuid
        grp = str(uuid.uuid4())
        db.add_all([
            Transaction(account_id=priv.id, category_id=cats["Bargeldbezug"].id, date=date(2026, 5, 2),
                        amount=Decimal("-200.00"), description="Bargeldbezug Bancomat",
                        management_type=ManagementType.TRANSFER, transfer_group_id=grp),
            Transaction(account_id=cash.id, category_id=cats["Bargeldbezug"].id, date=date(2026, 5, 2),
                        amount=Decimal("200.00"), description="Bargeldbezug Bancomat",
                        management_type=ManagementType.TRANSFER, transfer_group_id=grp),
        ])
        # Sparen-Übertrag
        grp2 = str(uuid.uuid4())
        db.add_all([
            Transaction(account_id=priv.id, category_id=cats["Kontoübertrag"].id, date=date(2026, 5, 26),
                        amount=Decimal("-500.00"), description="Übertrag aufs Sparkonto",
                        management_type=ManagementType.TRANSFER, transfer_group_id=grp2),
            Transaction(account_id=spar.id, category_id=cats["Kontoübertrag"].id, date=date(2026, 5, 26),
                        amount=Decimal("500.00"), description="Übertrag aufs Sparkonto",
                        management_type=ManagementType.TRANSFER, transfer_group_id=grp2),
        ])

        # Manuelles Abo + verbundene Buchungen
        db.add(ManualSubscription(name="KI-Abo", amount=Decimal("22.00"),
                                  interval=BudgetInterval.MONATLICH, kind="abo",
                                  match_keyword="musterdienst", category_id=cats["KI-Dienste"].id,
                                  account_id=priv.id))
        db.add(ManualSubscription(name="Miete", amount=Decimal("1450.00"),
                                  interval=BudgetInterval.MONATLICH, kind="fix",
                                  match_keyword="miete", category_id=cats["Miete"].id,
                                  account_id=priv.id))

        # Sparziel
        db.add(SavingsGoal(name="Notgroschen", target_amount=Decimal("10000"),
                           target_date=date(2026, 12, 31), account_id=spar.id,
                           description="3 Monatslöhne Reserve", icon="shield-lock"))
        db.add(SavingsGoal(name="Ferien Fernreise", target_amount=Decimal("4000"),
                           target_date=date(2027, 4, 1), account_id=spar.id,
                           description="Reisekasse", icon="beach"))

        # Standard-Budgets (Soll) für ein paar Kategorien
        for cname, amt, interval in [
            ("Lebensmittel", "450", BudgetInterval.MONATLICH),
            ("Miete", "1450", BudgetInterval.MONATLICH),
            ("Kaffee", "30", BudgetInterval.MONATLICH),
            ("Ferien", "2400", BudgetInterval.JAEHRLICH),
        ]:
            c = cats.get(cname)
            if c:
                db.add(StandardBudget(category_id=c.id, amount=Decimal(amt), interval=interval))

        # Monats-Budget-Override für aktuellen Monat
        db.add(Budget(category_id=cats["Lebensmittel"].id, month=date(2026, 5, 1),
                      planned_amount=Decimal("400"), is_auto_calculated=False))

        # ----------  Treffen-Fonds mit Ferienkonto  ----------
        # Damit die Rueckstellung im Browser ueberhaupt etwas zeigt: ein eigenes
        # Konto, drei bestaetigte Monatsraten, zwei davon ueberwiesen (also eine
        # offen), und eine abgeschlossene Reise mit echten Abfluessen.
        ferien = Account(name="Ferienkonto", type=AccountType.SAVINGS, currency="CHF",
                         opening_balance=Decimal("0"), current_balance=Decimal("0"),
                         sort_order=5, icon="beach")
        db.add(ferien)
        db.flush()
        for monat, betrag in ((date(2026, 3, 1), "300"), (date(2026, 4, 1), "300"),
                              (date(2026, 5, 1), "300")):
            db.add(MeetContribution(month=monat, person="a", amount_native=Decimal(betrag)))
            db.add(MeetContribution(month=monat, person="b", amount_native=Decimal("100")))
        for tag, betrag in ((date(2026, 3, 27), "300"), (date(2026, 4, 27), "300")):
            grp = str(uuid.uuid4())
            db.add_all([
                Transaction(account_id=priv.id, category_id=cats["Kontoübertrag"].id, date=tag,
                            amount=-Decimal(betrag), description="Rückstellung Ferien",
                            management_type=ManagementType.TRANSFER, transfer_group_id=grp),
                Transaction(account_id=ferien.id, category_id=cats["Kontoübertrag"].id, date=tag,
                            amount=Decimal(betrag), description="Rückstellung Ferien",
                            management_type=ManagementType.TRANSFER, transfer_group_id=grp),
            ])
        db.add(MeetVisit(date=date(2026, 4, 10), location="bei_b", nights=3,
                         cost_override_chf=Decimal("900")))
        db.add_all([
            Transaction(account_id=ferien.id, date=date(2026, 3, 30), amount=Decimal("-340.00"),
                        description="Flug Musterstadt"),
            Transaction(account_id=ferien.id, date=date(2026, 4, 11), amount=Decimal("-410.00"),
                        description="Unterkunft"),
        ])
        einstellungen = db.scalar(select(MeetFundSettings))
        if einstellungen is None:
            einstellungen = MeetFundSettings()
            db.add(einstellungen)
        einstellungen.start_month = date(2026, 1, 1)
        einstellungen.monthly_a_chf = Decimal("300")
        einstellungen.monthly_b_eur = Decimal("100")
        einstellungen.holiday_account_id = ferien.id


        # ----------  Verlaufswerte  ----------
        # ALLES ERFUNDEN. Zwei Reihen mit Absicht:
        #   * eine MONATLICHE mit Positionen, die bis fast heute laeuft -> das
        #     gestapelte Balkenbild und die beschriftete Zeitachse,
        #   * eine JAEHRLICHE, die 2023 aufhoert -> der leere Raum rechts, an dem
        #     man sieht, dass die Reihe stillsteht.
        # Ohne diese Werte zeigt die Verlaufsseite im Dev-Server gar nichts, und
        # jede Pruefung am Diagramm braucht erst einen Import von Hand.
        from moneten.dates import add_months as _plus
        from moneten.db.models import MetricPoint
        from moneten.db.models import MetricSeries as _MS

        reihen = {r.slug: r for r in db.scalars(select(_MS))}
        heute = date(2026, 5, 20)

        sw = reihen.get("beispielfunk")
        if sw is not None:
            start = date(2025, 3, 1)
            for i in range(14):
                s = _plus(start, i)
                abo, option = Decimal("64.90"), Decimal("39.90")
                rabatt = Decimal("-10.00") if i % 3 else Decimal("-20.00")
                tv = Decimal("12.00") if i > 4 else Decimal("0")
                wert = abo + option + tv + rabatt
                extras = {
                    "pos:Mobile-Abo": str(abo),
                    "pos:Internet-Option": str(option),
                    "pos:Kombi-Rabatt": str(rabatt),
                }
                if tv:
                    extras["pos:TV-Paket"] = str(tv)
                db.add(MetricPoint(
                    series_id=sw.id, period_start=s,
                    period_end=(_plus(s, 1) - timedelta(days=1)),
                    value=wert, source="Testrechnung", extras=extras))

        nv = reihen.get("nebenverdienst")
        if nv is not None:
            for jahr, betrag in ((2022, "1200.00"), (2023, "2400.00")):
                db.add(MetricPoint(
                    series_id=nv.id, period_start=date(jahr, 1, 1),
                    period_end=date(jahr, 12, 31),
                    value=Decimal(betrag), source="Testbeleg"))
        _ = heute

        db.flush()

        # ----------  Zwei Belege mit derselben Ware, anders geschrieben  ----------
        # Damit „Positionen vereinheitlichen" im Browser etwas zu entscheiden hat.
        # Der Lesefehler ist echt beobachtet (ein Buchstabe verdreht), die Ware
        # erfunden.
        for wann, name in ((date(2026, 5, 4), "Multifruchtsaft"),
                           (date(2026, 6, 9), "Muitifruchtsaft")):
            bon = Transaction(account_id=priv.id, date=wann, amount=Decimal("-2.65"),
                              description="Einkauf", category_id=cats["Lebensmittel"].id)
            db.add(bon)
            db.flush()
            db.add(Attachment(
                transaction_id=bon.id, file_path=None, original_name="Bon",
                parsed_items_json=json.dumps({
                    "merchant": "Grossverteiler", "amount": "2.65",
                    "items": [{"name": name, "price": "2.65", "qty": 1}],
                }, ensure_ascii=False),
            ))

        db.flush()

        # Konto-Salden aus Buchungen nachziehen.
        for acc in (priv, cash, spar, ferien):
            total = sum(
                (t.amount for t in db.scalars(
                    select(Transaction).where(Transaction.account_id == acc.id)).all()),
                Decimal("0"),
            )
            acc.current_balance = acc.opening_balance + total

        db.commit()


if __name__ == "__main__":
    _seed_dummy()
    if "--seed-only" in sys.argv:
        print("SEED OK")
        raise SystemExit(0)
    import uvicorn

    uvicorn.run("moneten.main:app", host="127.0.0.1", port=8000, log_level="warning")
