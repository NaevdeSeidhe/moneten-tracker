"""Offene Fixabgänge: was vom „Noch frei" schon vergeben ist.

Eigene Konten/Kategorien pro Test wären hier Overkill — entscheidend ist die
Isolation vom gemeinsamen Test-Bestand. Deshalb ein Stichmonat weit in der
Vergangenheit ohne Fremdbuchungen und Namen mit ``ZZZ``-Präfix.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import Account, BudgetInterval, ManualSubscription, Transaction
from moneten.db.session import SessionLocal
from moneten.services.committed import offene_fixabgaenge

# Ein Monat, in dem sonst keine Testdaten liegen.
MONAT = date(2016, 5, 1)
IM_MONAT = date(2016, 5, 20)


def _konto_id(db) -> int:
    """Irgendein vorhandenes Konto — welches, ist für diese Rechnung egal."""
    return db.scalars(select(Account.id)).first()


def _sub(db, name: str, betrag: str, *, kind: str = "abo",
         interval: BudgetInterval = BudgetInterval.MONATLICH) -> ManualSubscription:
    s = ManualSubscription(name=name, amount=Decimal(betrag), interval=interval,
                           kind=kind, match_keyword=name, is_active=True)
    db.add(s)
    return s


@contextmanager
def _aufraeumen(db, *objekte):
    """Loescht die angelegten Objekte ZWINGEND — auch wenn der Test scheitert.

    Vorher standen die ``db.delete``-Zeilen am Ende des Erfolgspfads. Ein
    fehlgeschlagener Test liess seine Abos in der gemeinsamen Test-Datenbank
    zurueck, und die tauchten dann in den Ergebnissen der folgenden Tests auf —
    ein Fehlschlag zog Folgefehler nach sich, die mit der Ursache nichts zu tun
    hatten.
    """
    try:
        yield objekte
    finally:
        for o in objekte:
            if o is not None:
                db.delete(o)
        db.commit()


def test_unbezahltes_abo_zaehlt_als_vergeben() -> None:
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixa{tag}", "80")
        db.commit()
        res = offene_fixabgaenge(db, MONAT, today=IM_MONAT)
        namen = {p["name"] for p in res["posten"]}
        assert s.name in namen
        db.delete(s)
        db.commit()


def test_bereits_gebuchtes_abo_wird_nicht_doppelt_gezaehlt() -> None:
    """Sonst zöge die App denselben Betrag zweimal ab: einmal im Ist, einmal hier."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixb{tag}", "80")
        db.commit()
        vorher = offene_fixabgaenge(db, MONAT, today=IM_MONAT)["summe"]

        tx = Transaction(account_id=_konto_id(db), date=date(2016, 5, 4), amount=Decimal("-80"), description=s.name)
        db.add(tx)
        db.commit()
        nachher = offene_fixabgaenge(db, MONAT, today=IM_MONAT)["summe"]

        assert nachher == vorher - Decimal("80")
        db.delete(tx)
        db.delete(s)
        db.commit()


def test_abweichender_betrag_gilt_nicht_als_bezahlt() -> None:
    """Eine Rückerstattung beim gleichen Händler erledigt die Fixkosten nicht."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixc{tag}", "80")
        tx = Transaction(account_id=_konto_id(db), date=date(2016, 5, 4), amount=Decimal("-3"), description=s.name)
        db.add_all([s, tx])
        db.commit()
        namen = {p["name"] for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]}
        assert s.name in namen
        db.delete(tx)
        db.delete(s)
        db.commit()


def test_jahresabo_zaehlt_mit_einem_zwoelftel() -> None:
    """Gleiche Rechenwelt wie die Soll-Seite des Budgets („inkl. 1/12")."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixd{tag}", "1200", interval=BudgetInterval.JAEHRLICH)
        db.commit()
        posten = [p for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]
                  if p["name"] == s.name]
        assert posten and posten[0]["betrag"] == Decimal("100.00")
        db.delete(s)
        db.commit()


def test_abgelaufener_monat_hat_nichts_offen() -> None:
    """Rückblickend steht nichts mehr aus — alles andere wäre nachträglich erfunden."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixe{tag}", "80")
        db.commit()
        res = offene_fixabgaenge(db, MONAT, today=date(2016, 7, 3))
        assert res["posten"] == [] and res["summe"] == Decimal("0")
        db.delete(s)
        db.commit()


def test_inaktives_abo_bleibt_aussen_vor() -> None:
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixf{tag}", "80")
        s.is_active = False
        db.commit()
        namen = {p["name"] for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]}
        assert s.name not in namen
        db.delete(s)
        db.commit()


def test_typischer_zahltag_aus_der_historie() -> None:
    """Median aus mindestens drei Beobachtungen; darunter lieber keine Angabe."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZfixg{tag}", "80")
        txs = [Transaction(account_id=_konto_id(db), date=date(2016, m, 25), amount=Decimal("-80"), description=s.name)
               for m in (2, 3, 4)]
        db.add_all([s, *txs])
        db.commit()
        posten = [p for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]
                  if p["name"] == s.name]
        assert posten and posten[0]["tag"] == 25
        for t in txs:
            db.delete(t)
        db.delete(s)
        db.commit()


def test_abo_name_trifft_abweichenden_banktext() -> None:
    """„KI-Dienste" muss „MUSTERDIENST KI VIA ZAHLDIENST" erkennen.

    Regressionstest: Der erste Entwurf verglich die Händler-Schlüssel auf
    Gleichheit. In den Testdaten galten dadurch Miete und das KI-Abo als offen,
    obwohl beide längst gebucht waren — die App hätte sie doppelt abgezogen.
    """
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = ManualSubscription(name=f"ZZZmusterx{tag} Pro", amount=Decimal("22"),
                               interval=BudgetInterval.MONATLICH, kind="abo", is_active=True)
        tx = Transaction(account_id=_konto_id(db), date=date(2016, 5, 8),
                         amount=Decimal("-22"),
                         description=f"MUSTERDIENST ZZZMUSTERX{tag} via Zahldienst")
        db.add_all([s, tx])
        db.commit()
        namen = {p["name"] for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]}
        assert s.name not in namen
        db.delete(tx)
        db.delete(s)
        db.commit()


# ---------------------------------------------------------------- Grenzfaelle


def test_unmittelbar_vergangener_monat_hat_nichts_offen() -> None:
    """Der Grenzfall an der Monatsgrenze — nicht zwei Monate daneben.

    Der erste Test dazu rechnete mit zwei Monaten Abstand; damit lieferten
    ``monatsende <= heute.replace(day=1)`` und ``<`` dasselbe Ergebnis. Kippt die
    Grenze, zoege die Budget-Seite im Rueckblick auf den Vormonat weiterhin alle
    „noch offenen" Fixabgaenge ab, obwohl der Monat vorbei ist.
    """
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZgrenz{tag}", "80")
        db.commit()
        with _aufraeumen(db, s):
            # MONAT ist Mai 2016, heute der 1. Juni: der Mai ist gerade vorbei.
            res = offene_fixabgaenge(db, MONAT, today=date(2016, 6, 1))
            assert res["posten"] == [] and res["summe"] == Decimal("0")


def test_zwei_beobachtungen_ergeben_noch_keinen_zahltag() -> None:
    """Aus zwei Buchungen laesst sich kein „typischer" Tag ableiten.

    Der Modul-Docstring schliesst das ausdruecklich als Rauschen aus; ohne diesen
    Test blieb die Schwelle ungeprueft (drei Beobachtungen waren der einzige Fall).
    """
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        s = _sub(db, f"ZZZzwei{tag}", "80")
        txs = [Transaction(account_id=_konto_id(db), date=date(2016, m, 25),
                           amount=Decimal("-80"), description=s.name)
               for m in (3, 4)]
        db.add_all([s, *txs])
        db.commit()
        with _aufraeumen(db, *txs, s):
            posten = [p for p in offene_fixabgaenge(db, MONAT, today=IM_MONAT)["posten"]
                      if p["name"] == s.name]
            assert posten, "Das Abo steht diesen Monat noch aus"
            assert posten[0]["tag"] is None, (
                "Zwei Beobachtungen sind kein Muster — lieber keine Angabe"
            )
