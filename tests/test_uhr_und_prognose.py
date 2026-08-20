"""Zwei Fehler, die nur unter bestimmten Umständen sichtbar wurden.

* Die Prognose fiel, während das Vermögen stieg — aber nur, solange der Lohn
  des laufenden Monats noch nicht gebucht war.
* Das Datum war falsch — aber nur zwischen Mitternacht und 02:00, und nur auf
  einem Server, der in UTC läuft. Also genau dort, wo niemand hinsieht.

Beides hat die Suite jahrelang nicht gemerkt, weil die Tests mit vollständigen
Monaten und mit der lokalen Systemuhr laufen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from moneten import dates


# ---------------------------------------------------------------------------
# N2 — „heute" auf einem Server, der in UTC tickt
# ---------------------------------------------------------------------------
class _FesteUhr:
    """Eine Uhr, die immer denselben Augenblick zeigt: 31.08.2026, 22:30 UTC.

    In ``Europe/Zurich`` ist das bereits der 1. September, 00:30. Genau dieses Fenster —
    Mitternacht bis 02:00 im Sommer — hat die App auf den Vormonat zeigen
    lassen.
    """

    augenblick = datetime(2026, 8, 31, 22, 30, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001 — Signatur von datetime.now
        return cls.augenblick.astimezone(tz) if tz else cls.augenblick.replace(tzinfo=None)


def test_heute_folgt_der_zeitzone_der_app_nicht_der_des_servers(monkeypatch) -> None:
    """Um 22:30 UTC ist in ``Europe/Zurich`` schon der nächste Tag — und Monat.

    ``date.today()`` hätte hier den 31.08. geliefert: Budget und Dashboard
    zeigten den August, obwohl der September begonnen hat. Am Jahreswechsel
    dasselbe mit dem Jahr, samt verschobener Grenze in der Jahresprobe des Lohns.
    """
    monkeypatch.setattr(dates, "datetime", _FesteUhr)
    monkeypatch.setattr(dates, "_zeitzone", lambda: "Europe/Zurich")

    assert dates.heute_lokal() == date(2026, 9, 1)
    # Zum Vergleich: die reine Serverzeit sagt noch August.
    assert _FesteUhr.augenblick.date() == date(2026, 8, 31)


def test_zeitzone_ist_einstellbar(monkeypatch) -> None:
    """Wer anderswo wohnt, stellt sie um — ohne den Quelltext anzufassen.

    Vorher stand die Zeitzone als Konstante im Modul. Für einen Teil der Nutzer
    war sie zufällig richtig, für alle anderen dauerhaft falsch.
    """
    monkeypatch.setattr(dates, "datetime", _FesteUhr)

    monkeypatch.setattr(dates, "_zeitzone", lambda: "UTC")
    assert dates.heute_lokal() == date(2026, 8, 31)

    monkeypatch.setattr(dates, "_zeitzone", lambda: "Pacific/Auckland")  # UTC+12
    assert dates.heute_lokal() == date(2026, 9, 1)


def test_unbekannte_zeitzone_legt_die_app_nicht_lahm(monkeypatch) -> None:
    """Ein Tippfehler in der Konfiguration darf kein Startfehler sein.

    Ohne diesen Rückfall stünde die App bei ``MONETEN_TIMEZONE=Europ/Zurich``
    still — und zwar mit einer Meldung aus der Tiefe von ``zoneinfo``, die
    niemandem sagt, wo er den Fehler gemacht hat.
    """
    monkeypatch.setattr(dates, "_zeitzone", lambda: "Kein/Ort")
    assert isinstance(dates.heute_lokal(), date)


# ---------------------------------------------------------------------------
# B7 — Prognose über einen halben Monat
# ---------------------------------------------------------------------------
def test_prognose_nimmt_den_laufenden_monat_nicht_als_stuetzpunkt() -> None:
    """Die Steigung darf nicht am unvollständigen Monat hängen.

    Gemessener Fall: sieben Monate mit je +1000, im laufenden fehlen 5000 Lohn.
    Die Steigung wurde mit **−200 pro Monat** gemeldet — die Linie fiel, während
    das Vermögen stieg. Nach zwölf Monaten 14'400 daneben, mit falschem
    Vorzeichen.

    Geprüft wird die Rechnung, nicht die Datenbank: die Reihe wird direkt
    vorgegeben, damit der Fall exakt der gemessene ist.
    """
    from moneten.services.forecasting import net_worth_projection

    # Fünf abgeschlossene Monate: +1000 je Monat. Der sechste ist der laufende
    # und liegt 5000 tiefer, weil der Lohn noch fehlt.
    reihe = [
        {"month": date(2026, m, 1), "value": Decimal(w)}
        for m, w in ((3, 12000), (4, 13000), (5, 14000), (6, 15000), (7, 16000), (8, 11000))
    ]

    import moneten.services.forecasting as fc

    echt = fc.net_worth_series
    fc.net_worth_series = lambda db, today, n=6: reihe  # noqa: ARG005
    try:
        p = net_worth_projection(None, date(2026, 8, 17), horizon=12, history=6)
    finally:
        fc.net_worth_series = echt

    # Fünf abgeschlossene Monate, +1000 je Schritt -> +1000.
    assert p.monthly_change == Decimal("1000"), f"Steigung: {p.monthly_change}"
    assert p.monthly_change > 0, "die Linie fiel, während das Vermögen stieg"


def test_prognose_bleibt_ruhig_bei_zu_wenig_daten() -> None:
    """Ein oder zwei Punkte ergeben keine Steigung — und keinen Absturz."""
    from moneten.services import forecasting as fc

    echt = fc.net_worth_series
    try:
        for reihe in ([{"month": date(2026, 8, 1), "value": Decimal("100")}], []):
            fc.net_worth_series = lambda db, today, n=6, r=reihe: r  # noqa: ARG005
            p = fc.net_worth_projection(None, date(2026, 8, 17), horizon=3, history=6)
            assert p.monthly_change == Decimal("0")
    finally:
        fc.net_worth_series = echt
