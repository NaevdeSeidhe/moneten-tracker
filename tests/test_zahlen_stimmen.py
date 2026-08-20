"""Drei Befunde aus dem unabhängigen Rechen- und Datenverlust-Durchgang.

Alle drei waren lautlos: falsche Zahlen ohne Fehlermeldung, ein verlorener
Rappen, eine leere Datenbank ohne Hinweis. Genau deshalb stehen sie hier — die
Suite war grün, während sie da waren.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Eine eigene Sitzung, die nach dem Test alles Angelegte wieder entfernt.

    Die Suite baut das Schema einmal je Lauf auf und teilt es zwischen den
    Tests. Wer Buchungen einfügt und stehen lässt, verfälscht die Zahlen der
    nächsten Datei — hier wird deshalb aufgeräumt.
    """
    with SessionLocal() as db:
        vorher_tx = {t.id for t in db.scalars(select(Transaction))}
        vorher_acc = {a.id for a in db.scalars(select(Account))}
        yield db
        db.rollback()
        for tx in db.scalars(select(Transaction)):
            if tx.id not in vorher_tx:
                db.delete(tx)
        for acc in db.scalars(select(Account)):
            if acc.id not in vorher_acc:
                db.delete(acc)
        db.commit()


def _konto(db: Session, name: str = "Probe") -> Account:
    k = Account(name=name, type=AccountType.BANK, currency="CHF",
                opening_balance=Decimal("10000"), current_balance=Decimal("10000"))
    db.add(k)
    db.flush()
    return k


# ---------------------------------------------------------------------------
# B3 — Monatszahlen ohne obere Datumsgrenze
# ---------------------------------------------------------------------------
def test_monatszahlen_zaehlen_die_zukunft_nicht_mit(db_session: Session) -> None:
    """Ein vorerfasster Dauerauftrag und ein Jahres-Tippfehler dürfen nicht mitzählen.

    Gemessen im Ausgangszustand: die Leitzahl meldete für August 2026 einen
    Saldo von 7'472.00, weil eine Buchung vom 10.09. und eine 
    mitgerechnet wurden. Richtig sind −1'750.00. Ein Tippfehler im Jahr wurde so
    zur grössten „Einnahme" des Monats.
    """
    from moneten.routers.dashboard import _month_totals

    heute = date(2026, 8, 17)
    # Gemessen wird die DIFFERENZ. Die Suite teilt eine Datenbank zwischen den
    # Dateien; absolute Summen waeren eine Aussage ueber alle Tests, nicht ueber
    # diesen. Genau daran ist dieser Test beim ersten Lauf haengengeblieben.
    vor_ein, vor_aus, vor_saldo = _month_totals(db_session, heute)

    k = _konto(db_session)
    for tag, betrag, text in [
        (date(2026, 8, 3), "-1500.00", "Miete"),
        (date(2026, 8, 16), "-250.00", "Einkauf"),
        (date(2026, 9, 10), "-777.00", "Dauerauftrag, vorerfasst"),
        (date(2027, 1, 15), "9999.00", "Jahres-Tippfehler"),
    ]:
        db_session.add(Transaction(account_id=k.id, date=tag,
                                   amount=Decimal(betrag), description=text))
    db_session.commit()

    eingang, ausgang, saldo = _month_totals(db_session, heute)
    assert eingang - vor_ein == Decimal("0.00"), f"Zukunft zählte als Einnahme: {eingang - vor_ein}"
    assert ausgang - vor_aus == Decimal("1750.00"), f"Zukunft zählte als Ausgabe: {ausgang - vor_aus}"
    assert saldo - vor_saldo == Decimal("-1750.00")


def test_leitzahl_und_kurve_sagen_dasselbe(db_session: Session) -> None:
    """Auf derselben Karte darf nicht zweierlei stehen.

    Die Kurve rechnet Monat-bis-heute (Tag 1 bis today.day). Die Leitzahl tat es
    nicht — beide Zahlen standen übereinander und widersprachen sich, und der
    Vergleichspfeil daneben verglich eine dritte Grösse.
    """
    from moneten.routers.dashboard import _month_totals, _monthly_series

    k = _konto(db_session, "Karte")
    for tag, betrag in [
        (date(2026, 8, 2), "-100.00"),
        (date(2026, 8, 20), "-50.00"),      # nach heute, aber im selben Monat
        (date(2026, 9, 1), "-999.00"),      # nächster Monat
    ]:
        db_session.add(Transaction(account_id=k.id, date=tag, amount=Decimal(betrag),
                                   description="x"))
    db_session.commit()

    # Hier ist der Vergleich in sich geschlossen: Leitzahl und Kurve muessen
    # dieselbe Zahl liefern, egal was sonst in der Datenbank steht.
    heute = date(2026, 8, 10)
    _, ausgang, saldo = _month_totals(db_session, heute)
    letzter = _monthly_series(db_session, heute, n=3)[-1]
    assert ausgang == letzter["expense"], f"Leitzahl {ausgang} ≠ Kurve {letzter['expense']}"
    assert saldo == letzter["saldo"]


# ---------------------------------------------------------------------------
# B1 — Median über float verliert einen Rappen
# ---------------------------------------------------------------------------
def test_median_rechnet_in_decimal_nicht_in_float() -> None:
    """Der Vorschlag für das Budget darf keinen Rappen verlieren.

    Gemessen: 11'358.17 und 8'444.82 ergaben 9901.49 statt 9901.50, weil der
    Median über ``float`` lief und dort ``9901.494999999999`` herauskam. An
    200'000 Zufallsfällen betraf das 5.6 %.
    """
    from moneten.services.median_budget import median_from_map

    imap = {(1, date(2026, 7, 1)): Decimal("11358.17"),
            (1, date(2026, 6, 1)): Decimal("8444.82")}
    assert median_from_map(imap, 1, date(2026, 8, 1)) == Decimal("9901.50")


def test_median_rundet_kaufmaennisch_und_bleibt_exakt() -> None:
    """Vier Werte, deren Mittel genau auf einem halben Rappen liegt.

    996.4950 muss zu 996.50 werden — mit ``float`` wurde daraus 996.49, und beim
    Füllen ganzer Franken kippte dadurch der Franken.
    """
    from moneten.services.median_budget import median_from_map

    imap = {(1, date(2026, 7, 1)): Decimal("1105.32"),
            (1, date(2026, 6, 1)): Decimal("704.44"),
            (1, date(2026, 5, 1)): Decimal("887.67"),
            (1, date(2026, 4, 1)): Decimal("1324.92")}
    assert median_from_map(imap, 1, date(2026, 8, 1)) == Decimal("996.50")


def test_median_einzelwert_und_leer() -> None:
    """Ein Wert ist sein eigener Median; keiner ergibt keinen Vorschlag."""
    from moneten.services.median_budget import median_from_map

    assert median_from_map({(1, date(2026, 7, 1)): Decimal("42.42")}, 1,
                           date(2026, 8, 1)) == Decimal("42.42")
    assert median_from_map({}, 1, date(2026, 8, 1)) is None


# ---------------------------------------------------------------------------
# S3 — leere Datenbank neben der vollen, ohne ein Wort
# ---------------------------------------------------------------------------
def test_warnung_wenn_eine_andere_datenbank_daneben_liegt(tmp_path, caplog) -> None:
    """Die App darf nicht stillschweigend eine leere Datenbank anlegen.

    Der gemessene Weg: ``cp .env.example .env`` setzt einen ABSOLUTEN Pfad auf
    ``moneten.db``. Der Rückfall auf den alten Namen greift dann bewusst nicht —
    eine Vermutung darf keine Angabe überstimmen. Die Folge war aber, dass die
    App eine leere Datenbank anlegte, während die volle mit dem alten Namen
    daneben lag: Konten da (aus den Vorgaben), Buchungen null, kein Hinweis.
    """
    from moneten.config import Settings

    (tmp_path / "bilanz.db").write_bytes(b"alte Daten")
    with caplog.at_level(logging.WARNING, logger="moneten.config"):
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'moneten.db').as_posix()}",
            secret_key="probe-schluessel-lang-genug-fuer-den-test",
            initial_pin="424242",
        )
    meldungen = " ".join(r.getMessage() for r in caplog.records)
    assert "bilanz.db" in meldungen, f"keine Warnung: {meldungen!r}"
    assert "LEERE" in meldungen


def test_keine_warnung_beim_ersten_start(tmp_path, caplog) -> None:
    """Eine frische Anlage hat keine Datenbank daneben — dann bleibt es still.

    Sonst wäre die Warnung beim allerersten Start jeder Installation zu sehen
    und damit wertlos.
    """
    from moneten.config import Settings

    with caplog.at_level(logging.WARNING, logger="moneten.config"):
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'moneten.db').as_posix()}",
            secret_key="probe-schluessel-lang-genug-fuer-den-test",
            initial_pin="424242",
        )
    meldungen = " ".join(r.getMessage() for r in caplog.records)
    assert "LEERE" not in meldungen, f"unnötige Warnung: {meldungen!r}"
