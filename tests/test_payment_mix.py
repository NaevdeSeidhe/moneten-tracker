"""Tests für „Bar gegen digital" und die Kassensturz-Erinnerung.

Die drei Entscheidungen, die das Ergebnis prägen, sind hier festgenagelt:
der Nenner (nur Alltagsausgaben), die Behandlung von Bargeldbezügen
(Umbuchung, keine Ausgabe) und die von Kassensturz-Korrekturen
(Bargeld — aber getrennt ausgewiesen).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from moneten.db.models import Account, AccountType, ManagementType, Transaction
from moneten.db.session import SessionLocal
from moneten.services.payment_mix import (
    KASSENSTURZ_PREFIX,
    _faellig_regel,
    kassensturz_faellig,
    payment_mix,
)

# Die Test-Datenbank ist über alle Tests hinweg dieselbe. Damit fremde Buchungen
# (die mit dem heutigen Datum angelegt werden) das Ergebnis nicht verfälschen,
# rechnen diese Tests in einem weit zurückliegenden Monat — dort liegen
# ausschliesslich die hier angelegten Buchungen.
STICHTAG = date(2019, 6, 15)


def _konten(db) -> tuple[int, int]:
    """Ein Bank- und ein Bargeld-Konto, frisch für den Test."""
    bank = Account(name=f"ZZZ-Bank-{uuid.uuid4().hex[:6]}", type=AccountType.BANK,
                   currency="CHF", opening_balance=Decimal("0"),
                   current_balance=Decimal("0"), sort_order=970)
    kasse = Account(name=f"ZZZ-Kasse-{uuid.uuid4().hex[:6]}", type=AccountType.CASH,
                    currency="CHF", opening_balance=Decimal("0"),
                    current_balance=Decimal("0"), sort_order=971)
    db.add_all([bank, kasse])
    db.commit()
    return bank.id, kasse.id


def _tx(db, konto_id: int, betrag: str, tag: date, mt=None, beschreibung="Test") -> None:
    db.add(Transaction(account_id=konto_id, date=tag, amount=Decimal(betrag),
                       description=beschreibung, management_type=mt))


def test_quote_zaehlt_nur_alltagsausgaben(logged_in_client) -> None:
    """Miete darf die Quote nicht verwässern — man kann sie nicht bar zahlen.

    Ohne diese Regel läge die Bar-Quote strukturell im niedrigen einstelligen
    Bereich und würde nur noch die Höhe der Fixkosten messen.
    """
    heute = STICHTAG
    with SessionLocal() as db:
        bank, kasse = _konten(db)
        _tx(db, bank, "-1450", heute, ManagementType.DAUERAUFTRAG, "Miete")   # zählt NICHT
        _tx(db, bank, "-100", heute, ManagementType.KOST_LOGIS, "Coop")       # digital
        _tx(db, kasse, "-100", heute, ManagementType.BARGELD, "Kaffee")       # bar
        db.commit()
        mix = payment_mix(db, heute)

    aktuell = mix["aktuell"]
    assert aktuell["bar"] == Decimal("100")
    assert aktuell["digital"] == Decimal("100")
    assert aktuell["pct"] == 50, "Miete hätte die Quote auf 6% gedrückt"


def test_bargeldbezug_ist_keine_ausgabe(logged_in_client) -> None:
    """Abheben ist eine Umbuchung — ausgegeben ist das Geld erst danach."""
    heute = date(2019, 8, 15)
    with SessionLocal() as db:
        bank, kasse = _konten(db)
        _tx(db, bank, "-200", heute, ManagementType.TRANSFER, "Bargeldbezug")
        _tx(db, bank, "-50", heute, ManagementType.KOST_LOGIS, "Coop")
        db.commit()
        mix = payment_mix(db, heute)

    assert mix["aktuell"]["digital"] == Decimal("50"), "der Bezug wurde mitgezählt"
    assert mix["aktuell"]["bar"] == Decimal("0")


def test_kassensturz_korrektur_zaehlt_als_bargeld_aber_getrennt(logged_in_client) -> None:
    """Vergessenes Bargeld ist Bargeld — die Quote darf Vergesslichkeit nicht
    als mangelnde Bargeld-Disziplin ausweisen. Getrennt ausgewiesen bleibt es
    trotzdem, weil eine hohe Zahl dort „erfasse sorgfältiger" bedeutet."""
    heute = date(2019, 10, 15)
    with SessionLocal() as db:
        bank, kasse = _konten(db)
        _tx(db, bank, "-100", heute, ManagementType.KOST_LOGIS, "Coop")
        _tx(db, kasse, "-40", heute, ManagementType.BARGELD, "Kaffee")
        _tx(db, kasse, "-60", heute, None, f"{KASSENSTURZ_PREFIX} (gezählt 0 CHF)")
        db.commit()
        mix = payment_mix(db, heute)

    a = mix["aktuell"]
    assert a["bar"] == Decimal("40"), "erfasstes Bargeld"
    assert a["bar_unerfasst"] == Decimal("60"), "Zähldifferenz getrennt"
    assert a["bar_total"] == Decimal("100")
    assert a["pct"] == 50, "die Zähldifferenz gehört in die Bar-Seite"
    assert mix["unerfasst_gesamt"] == Decimal("60")


def test_monat_ohne_ausgaben_hat_keine_quote(logged_in_client) -> None:
    """0 von 0 sind nicht 0 Prozent, sondern keine Aussage."""
    heute = date(2019, 12, 15)
    with SessionLocal() as db:
        _konten(db)
        mix = payment_mix(db, heute, monate=3)
    leer = [m for m in mix["monate"] if m["summe"] == 0]
    assert leer, "Testaufbau erwartet mindestens einen leeren Monat"
    assert all(m["pct"] is None for m in leer)


class TestKassensturzErinnerung:
    """Die Fälligkeitsregel als reine Funktion — ohne Datenbank und damit
    unabhängig davon, was andere Tests an Korrekturen hinterlassen haben."""

    def test_faellig_wenn_nie_gezaehlt(self) -> None:
        assert _faellig_regel(None, date(2026, 7, 3)) is True

    def test_nicht_faellig_direkt_nach_dem_zaehlen(self) -> None:
        """Am 30. gezählt, am 1. wäre es rein kalendarisch schon wieder fällig —
        genau das soll die Sieben-Tage-Sperre verhindern."""
        assert _faellig_regel(date(2026, 6, 30), date(2026, 7, 1)) is False

    def test_faellig_im_neuen_monat(self) -> None:
        assert _faellig_regel(date(2026, 6, 2), date(2026, 7, 1)) is True

    def test_nicht_faellig_im_selben_monat(self) -> None:
        assert _faellig_regel(date(2026, 7, 2), date(2026, 7, 28)) is False

    def test_ohne_bargeldkonto_nie_faellig(self, logged_in_client) -> None:
        """Integrationsprüfung der DB-Seite: die Funktion läuft und liefert die
        erwartete Form."""
        with SessionLocal() as db:
            ergebnis = kassensturz_faellig(db, date.today())
        assert set(ergebnis) == {"faellig", "letzter", "tage_her"}
        assert isinstance(ergebnis["faellig"], bool)


class TestErinnerungAufDerUebersicht:
    """WO die Erinnerung auf der Übersicht steht.

    Der Fehler: sie sass innerhalb der Karte „Bar bezahlt", und die hängt an
    ``mix.hat_daten``. Ohne Alltagsausgaben in zwölf Monaten fehlte die Karte —
    und mit ihr die Erinnerung. Genau dieser Zustand heisst aber „niemand hat
    mehr Bargeld gebucht", und genau dann ist Zählen fällig. Eine Erinnerung,
    die bei Bedarf verschwindet, ist keine.

    Gesteuert wird über die beiden Auswertungen, nicht über Buchungen: die
    Test-Datenbank ist geteilt, und „zwölf Monate ohne jede Alltagsausgabe"
    liesse sich darin nicht zuverlässig herstellen. Geprüft wird ohnehin die
    Vorlage, nicht die Rechnung — die steht in den Tests darüber.
    """

    #: Zustand „Karte fehlt": kein einziger Monat mit Alltagsausgaben.
    OHNE_DATEN = {"hat_daten": False}

    @staticmethod
    def _mix_mit_daten() -> dict:
        """Gerade so viel, wie die Karte zum Zeichnen braucht — erfundene Werte."""
        return {
            "hat_daten": True,
            "aktuell": {"pct": 40},
            "ziel_pct": 0,
            "monate": [{
                "label": "Jun", "jahr": 2026, "bar_total": Decimal("120"),
                "summe": Decimal("300"), "bar_unerfasst": Decimal("0"),
                "pct": 40, "pct_bar": 40.0, "pct_unerfasst": 0.0,
            }],
            "schnitt_pct": 40,
            "kategorien": [],
            "unerfasst_gesamt": Decimal("0"),
        }

    @staticmethod
    def _stelle(monkeypatch, mix: dict, faellig: bool,
                letzter: date | None = None, tage_her: int | None = None) -> None:
        from moneten.routers import dashboard

        monkeypatch.setattr(dashboard, "payment_mix", lambda *a, **k: mix)
        monkeypatch.setattr(dashboard, "kassensturz_faellig", lambda *a, **k: {
            "faellig": faellig, "letzter": letzter, "tage_her": tage_her,
        })
        # „Was kommt" wird MITgestellt, weil genau diese Karte zwischen den
        # beiden Ankern liegt, an denen die Position der Erinnerung haengt.
        # Ohne sie liesse sich der Block eine Karte tiefer schieben, ohne dass
        # etwas rot wird — und ob es die Karte gerade gibt, entschied bisher der
        # Bestand der geteilten Test-DB.
        monkeypatch.setattr(dashboard, "was_kommt", lambda *a, **k: {
            "posten": [], "summe": None, "fristen_stand": 2026, "veraltet": True,
        })

    def test_steht_auch_ohne_die_karte(self, logged_in_client, monkeypatch) -> None:
        """Der eigentliche Befund: keine Karte, trotzdem Erinnerung.

        Sie nimmt den Platz der fehlenden Karte ein — direkt hinter der
        Vermögensaufteilung. Dieselbe Stelle im Lesefluss, damit sie nicht von
        Monat zu Monat springt.
        """
        self._stelle(monkeypatch, self.OHNE_DATEN, faellig=True)
        seite = logged_in_client.get("/").text

        assert "Bar bezahlt" not in seite, "Vorbedingung: die Karte fehlt"
        assert "Was kommt" in seite, "Vorbedingung: die nächste Karte steht dahinter"
        assert "Kassensturz fällig" in seite, (
            "Die Erinnerung verschwindet mit ihrer Karte"
        )
        assert "mix-erinnerung is-solo" in seite, (
            "Allein stehend braucht sie den Abstand einer Karte"
        )
        vorher = seite.index("Vermögensaufteilung")
        erinnerung = seite.index("Kassensturz fällig")
        assert vorher < erinnerung < seite.index("Was kommt") < seite.index("flow-card"), (
            "Sie steht nicht am Platz ihrer Karte"
        )
        # UNMITTELBAR dahinter, nicht irgendwo davor: zwischen „Vermögens-
        # aufteilung" und dem Geldfluss liegt noch „Was kommt". Ohne diese Zeile
        # liess sich der Block an dieser Karte vorbei nach unten schieben — die
        # Reihenfolge oben stimmte weiter, ihr Platz war es nicht mehr.
        assert 'class="card' not in seite[vorher:erinnerung], (
            "Zwischen der Vermögenskarte und der Erinnerung beginnt eine weitere Karte"
        )

    def test_fuehrt_zu_den_konten(self, logged_in_client, monkeypatch) -> None:
        """Gezählt wird auf der Konten-Seite — ohne Ziel ist sie eine Sackgasse.

        Der Link hielt kein Test: er liess sich auf eine beliebige tote URL
        umbiegen, ohne dass etwas rot wurde.
        """
        self._stelle(monkeypatch, self.OHNE_DATEN, faellig=True)
        seite = logged_in_client.get("/").text

        ab = seite.index('<a class="mix-erinnerung')
        assert 'href="/accounts"' in seite[ab:seite.index(">", ab)], (
            f"Die Erinnerung führt nicht zu den Konten: {seite[ab:seite.index('>', ab)]!r}"
        )

    @pytest.mark.parametrize("mit_karte", [False, True])
    def test_nennt_den_tag_der_letzten_zaehlung(
            self, logged_in_client, monkeypatch, mit_karte: bool) -> None:
        """Der Alltagszweig — und der einzige, den bisher kein Test gerendert hat.

        „Noch nie gezählt" steht genau einmal im Leben der App da; danach immer
        die Zahl der Tage und das Datum. Geprüft war ausschliesslich der Zweig,
        den man praktisch nie sieht — an beiden Einbaustellen.
        """
        mix = self._mix_mit_daten() if mit_karte else self.OHNE_DATEN
        self._stelle(monkeypatch, mix, faellig=True,
                     letzter=date(2026, 5, 3), tage_her=42)
        # Der Zweig steht im Template über zwei Zeilen; für den Vergleich zählt
        # der Text, nicht seine Einrückung.
        seite = " ".join(logged_in_client.get("/").text.split())

        assert "zuletzt vor 42 Tagen" in seite, (
            "Die Erinnerung sagt nicht, wann zuletzt gezählt wurde"
        )
        assert "noch nie gezählt" not in seite

    def test_steht_genau_einmal_wenn_die_karte_da_ist(
            self, logged_in_client, monkeypatch) -> None:
        """Zweimal dieselbe Aufforderung liest man beim zweiten Mal nicht mehr.

        Ihr Platz ist dann die Karte: die Zählung macht die Quote darunter genau,
        und der Satz sagt das auch.
        """
        self._stelle(monkeypatch, self._mix_mit_daten(), faellig=True)
        seite = logged_in_client.get("/").text

        assert "Bar bezahlt" in seite, "Vorbedingung: die Karte ist da"
        assert seite.count("Kassensturz fällig") == 1, "Die Erinnerung steht doppelt"
        assert "macht die Quote unten genau" in seite
        assert "is-solo" not in seite, "In der Karte ist sie kein Einzelstück"

    def test_schweigt_wenn_nicht_faellig(self, logged_in_client, monkeypatch) -> None:
        """Ohne Fälligkeit kein Wort — sonst stünde sie dauerhaft da und wäre
        nach zwei Monaten unsichtbar geworden."""
        self._stelle(monkeypatch, self.OHNE_DATEN, faellig=False)
        seite = logged_in_client.get("/").text

        assert "Kassensturz fällig" not in seite
