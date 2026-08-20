"""Tests für den Steuerjahr-Auszug.

Die Seite rechnet keine Steuern — sie summiert. Getestet wird darum genau das:
richtiger Zeitraum, richtiges Vorzeichen, richtiger Stichtag fürs Vermögen.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    Category,
    ManagementType,
    Transaction,
    TransactionSplit,
)
from moneten.db.session import SessionLocal
from moneten.services.tax_year import steuerjahr

JAHR = 2017  # eigenes Testjahr, damit fremde Buchungen nicht hineinzählen


def test_summiert_nur_das_gewaehlte_jahr(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        cat = Category(name=f"ZZZ-Säule 3a-{uuid.uuid4().hex[:4]}",
                       management_type=ManagementType.SPAREN)
        konto = Account(name=f"ZZZ-Steuer-{uuid.uuid4().hex[:4]}", type=AccountType.BANK,
                        currency="CHF", opening_balance=Decimal("0"),
                        current_balance=Decimal("0"), sort_order=980)
        db.add_all([cat, konto])
        db.commit()
        for tag, betrag in [
            (date(JAHR, 3, 1), "-3000"),
            (date(JAHR, 11, 1), "-4000"),
            (date(JAHR - 1, 12, 31), "-9999"),   # Vorjahr — darf nicht zählen
            (date(JAHR + 1, 1, 1), "-8888"),     # Folgejahr — darf nicht zählen
        ]:
            db.add(Transaction(account_id=konto.id, category_id=cat.id, date=tag,
                               amount=Decimal(betrag), description="3a-Einzahlung"))
        db.commit()
        ergebnis = steuerjahr(db, JAHR)

    pos = next(p for p in ergebnis["positionen"] if p["titel"] == "Säule 3a")
    assert pos["betrag"] == Decimal("7000"), "nur die zwei Buchungen des Jahres"
    assert pos["anzahl"] == 2


def test_betrag_wird_positiv_ausgewiesen(logged_in_client: TestClient) -> None:
    """Ausgaben sind negativ gespeichert — im Auszug steht ein positiver Abzug.

    Die erste Fassung las nur die Positionen des Vorgaengertests und haette auch
    dann bestanden, wenn ueberall ``None`` gestanden haette: bei leerer Datenbank
    ist die Schleife leer und die Behauptung damit trivial wahr. Jetzt legt der
    Test seine eigene Ausgabe an und prueft eine konkrete Zahl.
    """
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        top = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
        kat = Category(name=f"Spende ZZZ{marke}", parent_id=top.id, sort_order=900,
                       management_type=top.management_type)
        db.add(kat)
        db.flush()
        tx = Transaction(account_id=konto.id, category_id=kat.id,
                         date=date(JAHR, 3, 3), amount=Decimal("-250"),
                         description="ZZZ Spende")
        db.add(tx)
        db.commit()

        pos = {p["titel"]: p for p in steuerjahr(db, JAHR)["positionen"]}
        assert pos["Spenden"]["betrag"] == Decimal("250"), (
            "minus 250 gebucht — im Auszug muss plus 250 als Abzug stehen"
        )
        assert all(p["betrag"] is None or p["betrag"] >= 0
                   for p in steuerjahr(db, JAHR)["positionen"])

        db.delete(tx)
        db.delete(kat)
        db.commit()


def test_vermoegen_ist_der_stand_per_jahresende(logged_in_client: TestClient) -> None:
    """Buchungen NACH dem 31.12. werden vom heutigen Saldo zurückgerechnet."""
    with SessionLocal() as db:
        konto = Account(name=f"ZZZ-Vermoegen-{uuid.uuid4().hex[:4]}", type=AccountType.BANK,
                        currency="CHF", opening_balance=Decimal("0"),
                        current_balance=Decimal("1000"), sort_order=981)
        db.add(konto)
        db.commit()
        # 400 kamen erst im Folgejahr dazu → per 31.12. waren es 600.
        db.add(Transaction(account_id=konto.id, date=date(JAHR + 1, 5, 1),
                           amount=Decimal("400"), description="später"))
        db.commit()
        ergebnis = steuerjahr(db, JAHR)

    zeile = next(k for k in ergebnis["konten"] if k["konto"].name == konto.name)
    assert zeile["saldo"] == Decimal("600")


def test_laufendes_jahr_wird_gekennzeichnet(logged_in_client: TestClient) -> None:
    """Fürs laufende Jahr sind die Zahlen unvollständig — das muss die Seite sagen."""
    with SessionLocal() as db:
        assert steuerjahr(db, date.today().year)["laufend"] is True
        assert steuerjahr(db, date.today().year - 1)["laufend"] is False


def test_seite_laedt(logged_in_client: TestClient) -> None:
    """Der Haftungsvorbehalt muss sichtbar bleiben — auch nach einer Umformulierung.

    Geprüft werden die zwei Aussagen, die ihn tragen, nicht ein Wortlaut: dass
    hier NICHT gerechnet wird, und wer stattdessen entscheidet. Vorher hing der
    Test an der Wendung „rechnet keine Steuern" und wurde rot, als der Satz beim
    Entschlacken gestrafft wurde — obwohl der Vorbehalt vollständig dastand. Ein
    Test, der die Formulierung festnagelt statt der Zusage, verhindert jede
    Verbesserung und schützt nichts.
    """
    resp = logged_in_client.get("/steuern")
    assert resp.status_code == 200
    assert "Steuerjahr" in resp.text
    assert "Keine Steuerberechnung" in resp.text, \
        "Der Vorbehalt sagt nicht mehr, dass hier nichts gerechnet wird"
    assert "Steuerbehörde" in resp.text, \
        "Der Vorbehalt sagt nicht mehr, wer über die Abzugsfähigkeit entscheidet"


# ---------------------------------------------------------------- Audit-Befunde


def _kat(db, name: str, eltern_typ=None):
    """Unterkategorie unter der ersten passenden Oberkategorie."""
    from sqlalchemy import select

    from moneten.db.models import Category

    top = db.scalars(select(Category).where(Category.parent_id.is_(None))).first()
    c = Category(name=name, parent_id=top.id, sort_order=900,
                 management_type=top.management_type)
    db.add(c)
    db.flush()
    return c


def test_aufgeteilte_buchung_faellt_nicht_unter_den_tisch() -> None:
    """Regressionstest: Splits haben am Kopf keine Kategorie.

    Die erste Fassung fragte ``Transaction.category_id`` direkt ab. Bei einer
    aufgeteilten Buchung ist die NULL — genau die Gesundheitsbelege, die der
    Quittungs-Scan routinemaessig aufteilt, fehlten damit still im Steuerauszug.
    Der Nutzer haette eine zu tiefe Zahl in die Steuererklaerung uebertragen.
    """
    import uuid
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    from moneten.db.models import Account, Transaction, TransactionSplit
    from moneten.db.session import SessionLocal
    from moneten.services.tax_year import steuerjahr

    JAHR = 2011
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        zahn = _kat(db, f"Zahnarzt ZZZ{marke}")
        apo = _kat(db, f"Apotheke ZZZ{marke}")
        koerper = _kat(db, f"ZZZneutral{marke}")

        normal = Transaction(account_id=konto.id, category_id=zahn.id,
                             date=date(JAHR, 4, 3), amount=Decimal("-300"),
                             description="ZZZ Kontrolle")
        geteilt = Transaction(account_id=konto.id, category_id=None, is_split=True,
                              date=date(JAHR, 6, 9), amount=Decimal("-200"),
                              description="ZZZ Bon")
        db.add_all([normal, geteilt])
        db.flush()
        db.add_all([
            TransactionSplit(transaction_id=geteilt.id, category_id=apo.id,
                             amount=Decimal("-120")),
            TransactionSplit(transaction_id=geteilt.id, category_id=koerper.id,
                             amount=Decimal("-80")),
        ])
        db.commit()

        pos = {p["titel"]: p for p in steuerjahr(db, JAHR)["positionen"]}
        gesundheit = pos["Gesundheitskosten"]
        assert gesundheit["betrag"] == Decimal("420"), (
            f"300 normal + 120 aus dem Split erwartet, bekommen {gesundheit['betrag']}"
        )
        assert gesundheit["anzahl"] == 2

        for s in db.scalars(select(TransactionSplit).where(
                TransactionSplit.transaction_id == geteilt.id)):
            db.delete(s)
        db.delete(normal)
        db.delete(geteilt)
        for c in (zahn, apo, koerper):
            db.delete(c)
        db.commit()


def test_ueberwiegende_rueckerstattung_ergibt_keinen_abzug() -> None:
    """Regressionstest: ``abs()`` machte aus einem Ueberschuss einen Abzug.

    Wer aus einer Position unterm Strich Geld zurueckbekommen hat, darf dort
    nichts abziehen. Die Netto-Summe ist dann positiv, und ``abs()`` haette
    daraus einen Abzug in genau dieser Hoehe gemacht.
    """
    import uuid
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    from moneten.db.models import Account, Transaction
    from moneten.db.session import SessionLocal
    from moneten.services.tax_year import steuerjahr

    JAHR = 2012
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        arzt = _kat(db, f"Arzt ZZZ{marke}")
        zahlung = Transaction(account_id=konto.id, category_id=arzt.id,
                              date=date(JAHR, 2, 1), amount=Decimal("-400"),
                              description="ZZZ Behandlung")
        erstattung = Transaction(account_id=konto.id, category_id=arzt.id,
                                 date=date(JAHR, 5, 1), amount=Decimal("700"),
                                 description="ZZZ Rueckerstattung")
        db.add_all([zahlung, erstattung])
        db.commit()

        pos = {p["titel"]: p for p in steuerjahr(db, JAHR)["positionen"]}
        assert pos["Gesundheitskosten"]["betrag"] == Decimal("0"), (
            "Netto +300 bekommen — das ist kein Abzug von 300"
        )

        db.delete(zahlung)
        db.delete(erstattung)
        db.delete(arzt)
        db.commit()


def test_seite_weicht_auf_ein_jahr_mit_daten_aus(logged_in_client) -> None:
    """Ohne ``jahr`` darf kein Jahr angezeigt werden, das kein Knopf anbietet."""
    from datetime import date

    resp = logged_in_client.get("/steuern")
    assert resp.status_code == 200
    # Das angezeigte Jahr steht in der Ueberschrift und muss unter den Knoepfen sein.
    import re
    m = re.search(r"Steuerjahr (\d{4})", resp.text)
    assert m, "Jahresueberschrift nicht gefunden"
    gezeigt = int(m.group(1))
    assert f'href="/steuern?jahr={gezeigt}"' in resp.text or gezeigt == date.today().year, (
        f"Jahr {gezeigt} wird angezeigt, ist aber nicht waehlbar"
    )


# ---------------------------------------------------------------------------
# Mehrjahres-Uebersicht
# ---------------------------------------------------------------------------
#
# EIGENE DATENBANK je Test: `steuer_uebersicht` fragt bewusst ohne Jahresfilter
# und sieht damit ALLE Buchungen der Datenbank. Auf der geteilten Test-DB haengt
# das Ergebnis davon ab, welche anderen Testmodule vorher gelaufen sind — die
# Behauptung „genau diese Spalten" waere dort nicht pruefbar, sondern Glueck.


@pytest.fixture
def eigene_db():
    """Leeres Schema in einer eigenen Datei — nur fuer diesen einen Test."""
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from moneten.db.models import Base

    with tempfile.TemporaryDirectory() as ordner:
        motor = create_engine(f"sqlite:///{Path(ordner) / 'uebersicht.db'}")
        Base.metadata.create_all(motor)
        with sessionmaker(bind=motor)() as db:
            yield db
        motor.dispose()


def _konto(db, saldo: str = "0") -> Account:
    k = Account(name="ZZZ-Konto", type=AccountType.BANK, currency="CHF",
                opening_balance=Decimal("0"), current_balance=Decimal(saldo))
    db.add(k)
    db.flush()
    return k


def _freie_kat(db, name: str) -> Category:
    c = Category(name=name, management_type=ManagementType.BARGELD)
    db.add(c)
    db.flush()
    return c


def test_uebersicht_ueberspringt_jahre_ohne_buchungen(eigene_db) -> None:
    """Ein leeres Jahr als Null-Balken hiesse „nichts ausgegeben".

    Tatsaechlich weiss die App ueber so ein Jahr gar nichts — die Aussage waere
    also nicht bloss ungenau, sondern falsch.
    """
    from moneten.services.tax_year import steuer_uebersicht

    konto = _konto(eigene_db, "1000")
    kat = _freie_kat(eigene_db, "ZZZ Säule 3a")
    for jahr in (2014, 2016):  # 2015 fehlt bewusst
        eigene_db.add(Transaction(account_id=konto.id, category_id=kat.id,
                                  date=date(jahr, 5, 4), amount=Decimal("-1000"),
                                  description="ZZZ"))
    eigene_db.commit()

    u = steuer_uebersicht(eigene_db)
    assert [s["jahr"] for s in u["jahre"]] == [2014, 2016]
    assert u["von"] == 2014 and u["bis"] == 2016, "Kartenkopf nennt die echte Spanne"


def test_uebersicht_zaehlt_aufgeteilte_buchungen_mit(eigene_db) -> None:
    """Derselbe Fallstrick wie im Einzeljahr: der Kopf traegt keine Kategorie.

    Waere er hier nicht beachtet, zeigte die Uebersicht eine andere Zahl als das
    Einzeljahr darunter — zwei Wahrheiten fuer dieselbe Frage.
    """
    from moneten.services.tax_year import steuer_uebersicht, steuerjahr

    konto = _konto(eigene_db, "1000")
    drei_a = _freie_kat(eigene_db, "ZZZ Säule 3a")
    apo = _freie_kat(eigene_db, "ZZZ Apotheke")
    rest = _freie_kat(eigene_db, "ZZZ Haushalt")

    eigene_db.add(Transaction(account_id=konto.id, category_id=drei_a.id,
                              date=date(2014, 3, 1), amount=Decimal("-500"),
                              description="ZZZ"))
    eigene_db.add(Transaction(account_id=konto.id, category_id=drei_a.id,
                              date=date(2015, 3, 1), amount=Decimal("-5000"),
                              description="ZZZ"))
    geteilt = Transaction(account_id=konto.id, category_id=None, is_split=True,
                          date=date(2015, 6, 9), amount=Decimal("-400"),
                          description="ZZZ Bon")
    eigene_db.add(geteilt)
    eigene_db.flush()
    eigene_db.add_all([
        TransactionSplit(transaction_id=geteilt.id, category_id=apo.id,
                         amount=Decimal("-250")),
        TransactionSplit(transaction_id=geteilt.id, category_id=rest.id,
                         amount=Decimal("-150")),
    ])
    eigene_db.commit()

    spalte = next(s for s in steuer_uebersicht(eigene_db)["jahre"] if s["jahr"] == 2015)
    assert spalte["abzuege"] == Decimal("5250"), (
        f"5000 Säule 3a + 250 Apotheke aus dem Split erwartet, bekommen {spalte['abzuege']}"
    )
    einzeln = steuerjahr(eigene_db, 2015)["positionen"]
    assert spalte["abzuege"] == sum(p["betrag"] for p in einzeln if p["betrag"]), (
        "Uebersicht und Einzeljahr muessen dieselbe Summe nennen"
    )


def test_kurvenpunkte_liegen_ueber_den_balken(eigene_db) -> None:
    """Die Kurve teilt sich die Jahresachse mit den Balken.

    Ihre x-Position ist die Spaltenmitte — und die stimmt nur, solange das
    Balkenraster keinen Spalt hat (siehe parts/steuer-uebersicht.css). Faellt
    diese Annahme, stehen Punkt und Balken sichtbar nebeneinander.
    """
    from moneten.services.tax_year import steuer_uebersicht

    konto = _konto(eigene_db, "9000")
    kat = _freie_kat(eigene_db, "ZZZ Säule 3a")
    for jahr in (2013, 2014, 2015, 2016):
        eigene_db.add(Transaction(account_id=konto.id, category_id=kat.id,
                                  date=date(jahr, 5, 4), amount=Decimal("-1000"),
                                  description="ZZZ"))
    eigene_db.commit()

    spalten = steuer_uebersicht(eigene_db)["jahre"]
    n = len(spalten)
    assert [s["vx"] for s in spalten] == [round((i + 0.5) / n * 100, 2) for i in range(n)]
    assert all(0 <= s["vy"] <= 100 for s in spalten), "kein Punkt ausserhalb des Feldes"


def test_uebersicht_schweigt_wenn_sie_nichts_zu_sagen_haette(eigene_db) -> None:
    """Ein einzelnes Jahr ist keine Entwicklung, und null ist keine Kurve."""
    from moneten.services.tax_year import steuer_uebersicht

    konto = _konto(eigene_db, "0")
    kat = _freie_kat(eigene_db, "ZZZ Haushalt")  # keine Steuerposition
    eigene_db.add(Transaction(account_id=konto.id, category_id=kat.id,
                              date=date(2014, 5, 4), amount=Decimal("-700"),
                              description="ZZZ"))
    eigene_db.commit()
    assert steuer_uebersicht(eigene_db) is None, "ein Jahr allein zeigt keine Entwicklung"

    # Zweites Jahr, aber jedes Jahr netto null → auch rueckgerechnet kein
    # Vermoegen, und ohne Steuerkategorie auch kein Abzug.
    eigene_db.add(Transaction(account_id=konto.id, category_id=kat.id,
                              date=date(2014, 6, 4), amount=Decimal("700"),
                              description="ZZZ"))
    for betrag in ("-900", "900"):
        eigene_db.add(Transaction(account_id=konto.id, category_id=kat.id,
                                  date=date(2016, 5, 4), amount=Decimal(betrag),
                                  description="ZZZ"))
    eigene_db.commit()
    assert steuer_uebersicht(eigene_db) is None, (
        "zwei leere Achsen und eine Nulllinie sind kein Diagramm"
    )


def test_vermoegenslinie_hat_eine_eigene_palettenfarbe() -> None:
    """Sonst sieht die Linie aus wie eine der gestapelten Positionen.

    Der Abstand zwischen den Farben ist nur INNERHALB der acht Palettentoene
    zugesichert (tests/test_chart_kontrast.py). Die Linie nimmt darum den
    letzten Slot — solange die Positionen ihn nicht selbst erreichen.
    """
    from moneten.services.tax_year import POSITIONEN, VERMOEGEN_SLOT

    assert len(POSITIONEN) <= VERMOEGEN_SLOT, (
        f"{len(POSITIONEN)} Positionen belegen Slot 0..{len(POSITIONEN) - 1} und "
        f"kollidieren mit der Vermoegenslinie auf Slot {VERMOEGEN_SLOT}"
    )


def test_uebersicht_verlinkt_ins_einzeljahr(logged_in_client) -> None:
    """Der Zweck der Uebersicht: von der Jahresreihe in ein einzelnes Jahr.

    Geprueft wird am gerenderten HTML, nicht am Modell — der Sprung existiert
    nur, wenn die Spalte auch wirklich ein <a> mit dem richtigen Ziel ist.
    """
    import re

    from moneten.services.tax_year import MAX_JAHRE_UEBERSICHT, jahre_mit_buchungen

    with SessionLocal() as db:
        jahre = jahre_mit_buchungen(db)[-MAX_JAHRE_UEBERSICHT:]
    assert len(jahre) >= 2, "Vorbedingung: die Testlaeufe davor haben mehrere Jahre angelegt"

    ziel = jahre[0]
    resp = logged_in_client.get(f"/steuern?jahr={ziel}")
    assert resp.status_code == 200
    verlinkt = re.findall(r'class="sj-spalte[^"]*"[^>]*?href="/steuern\?jahr=(\d{4})"', resp.text)
    assert [int(j) for j in verlinkt] == jahre, (
        f"jedes Jahr mit Buchungen braucht seine Spalte — {verlinkt} statt {jahre}"
    )
    assert re.search(rf'class="sj-spalte is-aktiv"[^>]*?href="/steuern\?jahr={ziel}"', resp.text), (
        f"das aufgeschlagene Jahr {ziel} muss in der Uebersicht markiert sein"
    )


def test_uebersicht_macht_aus_einer_rueckerstattung_keinen_balken() -> None:
    """Dieselbe Vorzeichenregel wie im Einzeljahr — aber fuer die Uebersicht.

    Die Uebersicht hat die Regel aus steuerjahr woertlich kopiert. Kopierte
    Regeln laufen auseinander: eine Mutation von -n if n < 0 zu abs(n)
    liess die ganze Suite gruen, weil nur die Einzeljahr-Fassung geprueft war.
    Ein Jahr mit ueberwiegender Rueckerstattung haette dann einen Balken in
    Hoehe des Ueberschusses bekommen — eine Ausgabe, die es nie gab.
    """
    import uuid
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    from moneten.db.models import Account, Transaction
    from moneten.db.session import SessionLocal
    from moneten.services.tax_year import steuer_uebersicht

    JAHR = 2013
    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db:
        konto = db.scalars(select(Account)).first()
        arzt = _kat(db, f"Arzt YYY{marke}")
        zahlung = Transaction(account_id=konto.id, category_id=arzt.id,
                              date=date(JAHR, 3, 1), amount=Decimal("-500"),
                              description="YYY Behandlung")
        erstattung = Transaction(account_id=konto.id, category_id=arzt.id,
                                 date=date(JAHR, 9, 1), amount=Decimal("900"),
                                 description="YYY Rueckerstattung")
        # Ein zweites Jahr mit einem echten Abzug: mit nur einem Jahr schweigt
        # die Uebersicht bewusst, und der Test haette nichts zu pruefen.
        nachbar = Transaction(account_id=konto.id, category_id=arzt.id,
                              date=date(JAHR + 1, 4, 1), amount=Decimal("-250"),
                              description="YYY Behandlung Folgejahr")
        # Die Erstattung kommt erst spaeter dazu — siehe Differenzmessung unten.
        db.add_all([zahlung, nachbar])
        db.commit()

        def abzuege_des_jahres() -> Decimal:
            # max_jahre ausdruecklich hoch: die Uebersicht zeigt sonst nur die
            # juengsten Jahre, und im Gesamtlauf legen andere Tests Buchungen in
            # spaeteren Jahren an — das Testjahr fiele hinten raus.
            u = steuer_uebersicht(db, max_jahre=50)
            assert u is not None
            treffer = [j for j in u["jahre"] if j["jahr"] == JAHR]
            assert treffer, f"Jahr {JAHR} fehlt in der Uebersicht"
            return treffer[0]["abzuege"]

        # Erst die Zahlung allein: 500 sind ein echter Abzug.
        vorher = abzuege_des_jahres()
        assert vorher >= Decimal("500"), f"Aufbau stimmt nicht, gemeldet {vorher}"

        # Dann die groessere Rueckerstattung dazu. Netto steht das Jahr bei +400,
        # also faellt der Abzug WEG. Mit abs() stuende hier 400 — ein Abzug in
        # Hoehe des Geldes, das zurueckkam.
        db.add(erstattung)
        db.commit()
        nachher = abzuege_des_jahres()

        assert nachher == Decimal("0"), (
            f"Netto +400 bekommen — das ist kein Abzug. Gemeldet: {nachher} "
            f"(400 hiesse: abs() statt Vorzeichenregel)"
        )

        db.delete(zahlung)
        db.delete(erstattung)
        db.delete(nachbar)
        db.delete(arzt)
        db.commit()
