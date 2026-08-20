"""Tests für den Treffen-Fonds: Kostenformel, Kurs-Umrechnung,
Bestätigungs-Toggle, Topf-Stand (inkl. vergangener Treffen), Prognose, Routen."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.dates import add_months
from moneten.db.models import MeetContribution, MeetFundSettings, MeetVisit
from moneten.db.session import SessionLocal
from moneten.services import meet_fund
from moneten.services.charts import curve_path


def _reset_meet() -> None:
    """Bestätigungen + Treffen leeren — die Tests teilen sich eine DB, und ein
    zweiter Confirm-POST wäre sonst ein Toggle (trägt wieder aus)."""
    with SessionLocal() as db:
        for model in (MeetContribution, MeetVisit):
            for row in db.scalars(select(model)):
                db.delete(row)
        db.commit()


def test_visit_cost_formula() -> None:
    """Auswärts (bei_b): Flug + Airbnb×Nächte + Verpflegung×(Nächte+1);
    daheim (bei_a): ohne Airbnb.
    Längerer Besuch skaliert anteilig (Flug fix, Nächte/Tage mehr)."""
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        # Defaults: Flug 300 · Airbnb 100/Nacht · Verpflegung 30/Tag
        assert meet_fund.visit_cost_chf(s, "bei_b", 3) == Decimal("720.00")   # 300+300+120
        assert meet_fund.visit_cost_chf(s, "bei_a", 3) == Decimal("420.00")   # 300+120
        assert meet_fund.visit_cost_chf(s, "bei_b", 6) == Decimal("1110.00")  # 300+600+210
        assert meet_fund.visit_cost_chf(s, "bei_b", 3, Decimal("999")) == Decimal("999.00")


def test_monthly_total_and_rate() -> None:
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        # 300 CHF + 100 € × 0.95 = 395.00
        assert meet_fund.monthly_total_chf(s) == Decimal("395.00")
        s.eur_chf_rate = Decimal("1.00")
        assert meet_fund.monthly_total_chf(s) == Decimal("400.00")


def test_confirm_toggle_and_balance(logged_in_client: TestClient) -> None:
    _reset_meet()
    month = date.today().replace(day=1).isoformat()
    # Bestätigen: Person A + Person B
    for person in ("a", "b"):
        resp = logged_in_client.post("/savings-goals/meet/confirm",
                                     data={"month": month, "person": person})
        assert resp.status_code == 200
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        bal = meet_fund.fund_balance(db, s, date.today())
        assert bal["a_chf"] == Decimal("300.00")
        assert bal["b_eur"] == Decimal("100.00")
        assert bal["total"] == Decimal("395.00")  # 300 + 100×0.95
    # Nochmal klicken = wieder austragen
    resp = logged_in_client.post("/savings-goals/meet/confirm",
                                 data={"month": month, "person": "b"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(MeetContribution).where(MeetContribution.person == "b")) is None
        s = meet_fund.get_settings(db)
        assert meet_fund.fund_balance(db, s, date.today())["total"] == Decimal("300.00")


def test_past_visit_reduces_balance(logged_in_client: TestClient) -> None:
    _reset_meet()
    month = date.today().replace(day=1).isoformat()
    logged_in_client.post("/savings-goals/meet/confirm", data={"month": month, "person": "a"})
    # Vergangenes Treffen auswärts (3 Nächte = 720) → Topf geht ins Minus.
    resp = logged_in_client.post("/savings-goals/meet/visit",
                                 data={"visit_date": "2026-06-05", "location": "bei_b", "nights": "3"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        bal = meet_fund.fund_balance(db, s, date.today())
        assert bal["spent"] == Decimal("720.00")
        assert bal["total"] == Decimal("300.00") - Decimal("720.00")


def test_projection_dips_at_future_visit() -> None:
    _reset_meet()
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        today = date(2026, 7, 3)
        db.add(MeetVisit(date=date(2026, 10, 10), location="bei_a", nights=3))
        db.commit()
        p = meet_fund.projection(db, s, today, horizon=6)
        assert len(p["series"]) == 7  # heute + 6 Monate
        assert len(p["markers"]) == 1
        # Oktober = Index 3 (Aug=1, Sep=2, Okt=3): Zuwachs 395 − Besuch 420 → Dip.
        assert p["markers"][0]["idx"] == 3
        assert p["series"][3] == p["series"][2] + Decimal("395.00") - Decimal("420.00")


def test_prognose_kurve_ist_geklemmt() -> None:
    """Die Prognose nutzt dieselbe geklemmte Rundung wie der Vermögens-Verlauf.

    An einem Treffen knickt die Kurve scharf ab. Ungeklemmt liefe die Glättung
    dort über den Knickwert hinaus und zeigte einen Stand, den der Topf nie hat —
    darum muss der Pfad exakt dem von ``curve_path(..., klemmen=True)``
    entsprechen und sich vom ungeklemmten unterscheiden.
    """
    _reset_meet()
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        db.add(MeetVisit(date=date(2026, 10, 10), location="bei_b", nights=3))
        db.commit()
        p = meet_fund.projection(db, s, date(2026, 7, 3), horizon=6)
        pts = p["geo"]["pts"]
        assert p["geo"]["line"] == curve_path(pts, klemmen=True)
        assert p["geo"]["line"] != curve_path(pts)
        # Die Fläche benutzt dieselben Segmente und schliesst auf der
        # Grundlinie. Der Wert wird ABGELEITET, nicht eingetippt: als der Rand
        # wegen angeschnittener Flaggen von 10 auf 16 wuchs, scheiterte dieser
        # Test an der festen 130 — obwohl die Geometrie richtig war. Ein Test,
        # der bei jeder Randänderung von Hand nachgezogen werden muss, prüft
        # den Rand statt die Sache.
        boden = meet_fund.PROG_H - meet_fund.PROG_PAD
        assert p["geo"]["area"].endswith(f"L {pts[-1][0]},{boden} Z")
    _reset_meet()


def test_jar_stat_ist_ein_topf() -> None:
    """Ein Glas statt zweier — Bezugsgrösse ist das nächste geplante Treffen.

    Früher lieferte ``jar_stats`` zwei Gläser (eines je Reiseziel). Das liest
    sich als zwei Kassen; der Topf ist aber einer. Hier steht, was stattdessen
    gilt: ohne Plan zählt der TEURERE Standard-Besuch (ein volles Glas soll für
    beide Richtungen reichen), mit Plan dessen echte Kosten.
    """
    _reset_meet()
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        today = date(2026, 7, 3)
        teurer = max(
            meet_fund.visit_cost_chf(s, loc, s.default_nights) for loc in meet_fund.LOCATIONS
        )
        jar = meet_fund.jar_stat(db, s, teurer * 2, today)
        assert jar["visit"] is None
        assert jar["cost"] == teurer
        assert jar["pct"] == 100  # gedeckelt, obwohl der Topf für 2 Besuche reicht
        assert jar["visits"] == 2

        # Vergangene Treffen sind kein Ziel mehr — nur das nächste künftige zählt.
        db.add(MeetVisit(date=date(2026, 5, 1), location="bei_b", nights=9))
        db.add(MeetVisit(date=date(2026, 9, 12), location="bei_a", nights=3))
        db.commit()
        kosten = meet_fund.visit_cost_chf(s, "bei_a", 3)
        jar = meet_fund.jar_stat(db, s, kosten / 2, today)
        assert jar["visit"] is not None
        assert jar["visit"].date == date(2026, 9, 12)
        assert jar["location"] == "bei_a"
        assert jar["cost"] == kosten
        assert jar["pct"] == 50
        assert jar["visits"] == 0
    _reset_meet()


def test_settings_route_updates_factors(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/savings-goals/meet/settings", data={
        "eur_chf_rate": "0.95", "airbnb_night_chf": "120", "default_nights": "4",
    })
    assert resp.status_code == 200
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        assert s.eur_chf_rate == Decimal("0.95")
        assert s.airbnb_night_chf == Decimal("120.00")
        assert s.default_nights == 4
    # Ungültiger Wert → 400, nichts geändert
    resp = logged_in_client.post("/savings-goals/meet/settings", data={"eur_chf_rate": "abc"})
    assert resp.status_code == 400


def test_visit_add_delete_and_page_renders(logged_in_client: TestClient) -> None:
    _reset_meet()
    resp = logged_in_client.post("/savings-goals/meet/visit",
                                 data={"visit_date": "2027-01-15", "location": "bei_b", "nights": "6"})
    assert resp.status_code == 200
    assert "15.01.2027" in resp.text
    with SessionLocal() as db:
        vid = db.scalar(select(MeetVisit.id).where(MeetVisit.date == date(2027, 1, 15)))
    assert vid is not None
    page = logged_in_client.get("/savings-goals")
    assert "Treffen mit Penelope" in page.text
    assert page.text.count("jar-svg") == 1  # EIN Glas, nicht zwei
    assert "davon Odysseus" in page.text  # Herkunft bleibt sichtbar
    resp = logged_in_client.post(f"/savings-goals/meet/visit/{vid}/delete")
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.get(MeetVisit, vid) is None


# ---------------------------------------------------------------------------
# Abweichender Betrag + vergangene Monate nachtragen
# ---------------------------------------------------------------------------


def _setze_startmonat(m: date) -> date:
    """Setzt den Fonds-Start und liefert den vorherigen Wert zum Zurücksetzen."""
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        alt = s.start_month
        s.start_month = m
        db.commit()
        return alt


def test_abweichender_betrag_ersetzt_die_geplante_rate(logged_in_client: TestClient) -> None:
    """Der Kern des Befunds: ein knapper Monat muss eintragbar sein.

    Der Haken allein friert immer die GEPLANTE Rate ein. Wer im August nur 200
    statt 345 zurücklegen konnte, hatte bisher nur die Wahl zwischen „345" und
    „gar nichts" — der Topf zeigte danach dauerhaft einen Stand, den es nie gab.
    """
    _reset_meet()
    monat = date.today().replace(day=1).isoformat()
    resp = logged_in_client.post("/savings-goals/meet/amount",
                                 data={"month": monat, "person": "a", "amount": "200"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        c = db.scalar(select(MeetContribution).where(MeetContribution.person == "a"))
        assert c is not None
        assert c.amount_native == Decimal("200.00")
        s = meet_fund.get_settings(db)
        assert meet_fund.fund_balance(db, s, date.today())["a_chf"] == Decimal("200.00")

    # Zweiter Eintrag überschreibt, statt eine zweite Zeile anzulegen.
    logged_in_client.post("/savings-goals/meet/amount",
                          data={"month": monat, "person": "a", "amount": "260.50"})
    with SessionLocal() as db:
        alle = list(db.scalars(select(MeetContribution).where(MeetContribution.person == "a")))
        assert len(alle) == 1
        assert alle[0].amount_native == Decimal("260.50")

    # 0 bzw. leer heisst „nichts zurückgelegt" — der Eintrag verschwindet.
    logged_in_client.post("/savings-goals/meet/amount",
                          data={"month": monat, "person": "a", "amount": "0"})
    with SessionLocal() as db:
        assert db.scalar(select(MeetContribution).where(MeetContribution.person == "a")) is None
    _reset_meet()


def test_vergangener_monat_ist_nachtragbar(logged_in_client: TestClient) -> None:
    """Rückwirkend eintragen — und nur innerhalb der angebotenen Spanne.

    Ein Beitrag ausserhalb der Spanne zählte im Topf mit, stünde aber in keiner
    Monatsliste: der Stand wäre nicht mehr herleitbar.
    """
    _reset_meet()
    heute = date.today().replace(day=1)
    vergangen = add_months(heute, -2)
    alt = _setze_startmonat(add_months(heute, -6))
    try:
        resp = logged_in_client.post("/savings-goals/meet/amount",
                                     data={"month": vergangen.isoformat(), "person": "b",
                                           "amount": "80"})
        assert resp.status_code == 200
        with SessionLocal() as db:
            c = db.scalar(select(MeetContribution).where(MeetContribution.month == vergangen))
            assert c is not None and c.amount_native == Decimal("80.00")
        # Die Monatsliste bleibt offen: jede Aktion tauscht #savings-root aus,
        # sonst klappte sie bei jedem nachgetragenen Monat wieder zu.
        assert '<details class="disclosure" open>' in resp.text

        # Auch der Haken greift rückwirkend.
        resp = logged_in_client.post("/savings-goals/meet/confirm",
                                     data={"month": add_months(heute, -3).isoformat(),
                                           "person": "a"})
        assert resp.status_code == 200

        # Vor dem Fonds-Start: abgelehnt, nichts gespeichert.
        zu_frueh = add_months(heute, -12)
        resp = logged_in_client.post("/savings-goals/meet/amount",
                                     data={"month": zu_frueh.isoformat(), "person": "a",
                                           "amount": "50"})
        assert resp.status_code == 400
        with SessionLocal() as db:
            assert db.scalar(select(MeetContribution).where(MeetContribution.month == zu_frueh)) is None
    finally:
        _setze_startmonat(alt)
        _reset_meet()


def test_start_monat_oeffnet_die_vergangenheit(logged_in_client: TestClient) -> None:
    """Ohne einstellbaren Start beginnt die Monatsliste einfach später.

    Genau daran scheiterte das Nachtragen: die Oberfläche bot die Monate vor
    dem eingestellten Start gar nicht erst an.
    """
    _reset_meet()
    heute = date.today().replace(day=1)
    alt = _setze_startmonat(heute)
    try:
        with SessionLocal() as db:
            s = meet_fund.get_settings(db)
            vorher = len(meet_fund.month_rows(db, s, date.today()))
        resp = logged_in_client.post("/savings-goals/meet/settings",
                                     data={"start_month": add_months(heute, -5).strftime("%Y-%m")})
        assert resp.status_code == 200
        with SessionLocal() as db:
            s = meet_fund.get_settings(db)
            assert s.start_month == add_months(heute, -5)
            assert len(meet_fund.month_rows(db, s, date.today())) == vorher + 5
        resp = logged_in_client.post("/savings-goals/meet/settings", data={"start_month": "Unfug"})
        assert resp.status_code == 400
    finally:
        _setze_startmonat(alt)
        _reset_meet()


def test_monatszeile_trennt_eingetragen_und_geplant() -> None:
    """``field`` ist leer, solange nichts eingetragen ist — ``placeholder`` zeigt den Plan.

    Stünde der Plan als Wert im Feld, sähe jeder offene Monat wie ein erledigter
    aus, und ein Haken wäre nicht mehr von einer blossen Absicht zu unterscheiden.
    """
    _reset_meet()
    heute = date.today().replace(day=1)
    alt = _setze_startmonat(heute)
    try:
        with SessionLocal() as db:
            db.add(MeetContribution(month=heute, person="a", amount_native=Decimal("200")))
            db.commit()
            s = meet_fund.get_settings(db)
            zeile = next(r for r in meet_fund.month_rows(db, s, date.today()) if r["current"])
            a_wert, b_wert = zeile["persons"]
            assert a_wert["confirmed"] is True
            assert a_wert["field"] == "200"          # ohne die zwei Nullen
            assert a_wert["abweichend"] is True      # 200 ≠ geplante Rate
            assert b_wert["confirmed"] is False
            assert b_wert["field"] == ""
            assert b_wert["placeholder"] == meet_fund.feld_betrag(s.monthly_b_eur)
            assert zeile["chf_sum"] == Decimal("200.00")  # nur Eingetragenes zählt
    finally:
        _setze_startmonat(alt)
        _reset_meet()


def test_jar_stat_nennt_den_fehlbetrag() -> None:
    """„noch nicht gedeckt" sagte nicht, um wie viel es geht."""
    _reset_meet()
    with SessionLocal() as db:
        s = meet_fund.get_settings(db)
        heute = date(2026, 7, 3)
        jar = meet_fund.jar_stat(db, s, Decimal("100"), heute)
        assert jar["missing"] == jar["cost"] - Decimal("100")
        assert meet_fund.jar_stat(db, s, jar["cost"] * 2, heute)["missing"] == Decimal("0")


# ---------------------------------------------------------------------------
# Startmonat: die Vergangenheit steht offen, ohne sie zuzumauern
# ---------------------------------------------------------------------------


def _offene_kaesten(html: str) -> list[str]:
    """Beschriftungen der aufgeklappten Aufklapper — welcher Kasten offen steht.

    Die blosse Anwesenheit von ``open`` sagt es nicht: „Weitere Monate" und
    „Faktoren anpassen" tragen dasselbe Markup.
    """
    return re.findall(r'<details class="disclosure" open>\s*<summary[^>]*>([^<·]+)', html)


def test_fonds_start_ist_abgeleitet_nicht_fest_verdrahtet() -> None:
    """Die Vorgabe für den Startmonat wird beim ersten Zugriff abgeleitet.

    Als festes Datum veraltet sie lautlos: sie bleibt stehen, während die
    Gegenwart weiterläuft. Der frühere Wert (1.7.2026) liess im August 2026
    genau EINEN vergangenen Monat nachtragen — und mit jedem weiteren Monat
    wäre die Lücke zwischen Vorgabe und Gegenwart grösser geworden.
    """
    with SessionLocal() as db:
        neu = MeetFundSettings()
        db.add(neu)
        db.flush()  # erst beim INSERT greift die Vorgabe
        start = neu.start_month
        db.rollback()  # die geteilte Test-DB behält ihre eine Zeile
    assert start == date(date.today().year, 1, 1)


def test_start_grenze_haelt_erfasste_beitraege_in_der_liste() -> None:
    """Der Startmonat darf nicht hinter einen erfassten Beitrag rutschen.

    ``_meet_month`` lehnt einen Beitrag ausserhalb der Spanne ab, weil er im
    Topf mitzählte und in keiner Liste stünde. Verschieben liess sich die Spanne
    trotzdem — derselbe Zustand, nur über einen anderen Weg erreicht.
    """
    _reset_meet()
    heute = date.today().replace(day=1)
    with SessionLocal() as db:
        assert meet_fund.start_grenze(db, date.today()) == heute
        db.add(MeetContribution(month=add_months(heute, -4), person="b",
                                amount_native=Decimal("80")))
        db.commit()
        assert meet_fund.start_grenze(db, date.today()) == add_months(heute, -4)
    _reset_meet()


def test_startmonat_kann_erfasste_monate_nicht_ueberholen(logged_in_client: TestClient) -> None:
    """Beide Grenzen greifen in der Route, und die Meldung nennt sie."""
    _reset_meet()
    heute = date.today().replace(day=1)
    alt = _setze_startmonat(add_months(heute, -6))
    try:
        vergangen = add_months(heute, -4)
        logged_in_client.post("/savings-goals/meet/amount",
                              data={"month": vergangen.isoformat(), "person": "b",
                                    "amount": "80"})
        resp = logged_in_client.post("/savings-goals/meet/settings",
                                     data={"start_month": add_months(heute, -3).strftime("%Y-%m")})
        assert resp.status_code == 400
        assert meet_fund.monats_label(vergangen) in resp.text
        assert "in keiner Liste" in resp.text

        # Ohne Beitrag greift die andere Grenze: hinter dem laufenden Monat
        # begänne die Liste in der Zukunft und wäre leer.
        _reset_meet()
        resp = logged_in_client.post("/savings-goals/meet/settings",
                                     data={"start_month": add_months(heute, 2).strftime("%Y-%m")})
        assert resp.status_code == 400
        assert meet_fund.monats_label(heute) in resp.text
        assert "in der Zukunft" in resp.text
        with SessionLocal() as db:
            assert meet_fund.get_settings(db).start_month == add_months(heute, -6)
    finally:
        _setze_startmonat(alt)
        _reset_meet()


def test_startmonat_steht_in_der_monatsliste(logged_in_client: TestClient) -> None:
    """Der Hebel steht dort, wo der Monat fehlt — nicht nur unter „Faktoren".

    Wer die Liste aufklappt und seinen Monat nicht findet, hatte bisher keinen
    Hinweis, dass der Fonds-Start ihn zurückhält. Und nach dem Verschieben muss
    die Liste offen bleiben: sonst sieht man das Ergebnis erst nach erneutem
    Aufklappen.
    """
    _reset_meet()
    heute = date.today().replace(day=1)
    alt = _setze_startmonat(add_months(heute, -1))
    try:
        seite = logged_in_client.get("/savings-goals").text
        liste = seite.split('class="meet-history"')[1].split("</details>")[0]
        assert 'name="start_month"' in liste
        assert 'hx-post="/savings-goals/meet/settings"' in liste
        # Der Browser soll gar nicht erst anbieten, was die Route ablehnt.
        assert f'max="{heute.strftime("%Y-%m")}"' in liste

        # Abgeschickt wird, was das Formular WIRKLICH mitgibt — sonst prüft der
        # Test seine eigene Annahme statt des Kastens.
        frueher = add_months(heute, -7)
        resp = logged_in_client.post(
            "/savings-goals/meet/settings",
            data={"start_month": frueher.strftime("%Y-%m"),
                  "offen": re.search(r'name="offen" value="([^"]+)"', liste).group(1)},
        )
        assert resp.status_code == 200
        assert meet_fund.monats_label(frueher) in resp.text
        # Sechs Monate mehr in der Liste — gezählt, nicht nur beschriftet.
        assert (resp.text.count('class="meet-history-row')
                == seite.count('class="meet-history-row') + 6)
        assert [k.strip() for k in _offene_kaesten(resp.text)] == ["Weitere Monate"]
    finally:
        _setze_startmonat(alt)
        _reset_meet()


def test_seite_zeigt_marken_und_waehrung(logged_in_client: TestClient) -> None:
    """Die drei sichtbaren Befunde am Markup nachgewiesen.

    Farbkreise (`meet-dot`) sind durch runde Marken ersetzt, die Leitzahl trägt
    ihre Währung, und die Prozentzahl steht nicht mehr als <text> im Glas.

    Die Marke trug einmal zwei Landesflaggen. Sie waren selbsterklärend — und
    verrieten zwei Länder. Jetzt steht der Anfangsbuchstabe der Person darin,
    bei der man sich trifft, und der Name kommt aus den Einstellungen.
    """
    _reset_meet()
    # Beide Richtungen, damit auch beide Marken im Markup landen.
    logged_in_client.post("/savings-goals/meet/visit",
                          data={"visit_date": "2027-03-12", "location": "bei_b", "nights": "4"})
    logged_in_client.post("/savings-goals/meet/visit",
                          data={"visit_date": "2027-06-04", "location": "bei_a", "nights": "3"})
    seite = logged_in_client.get("/savings-goals").text
    assert 'class="meet-dot' not in seite
    assert 'class="meet-flag"' in seite
    # Kein Landesbezug mehr im Markup — weder als Farbe noch als Name.
    assert "--flag-nl" not in seite and "--flag-ch" not in seite
    # Die beiden Anfangsbuchstaben der Standardnamen („Ich" / „Partner").
    assert 'class="meet-marke-text">O<' in seite
    assert 'class="meet-marke-text">P<' in seite
    assert seite.count('id="meet-flag-kreis"') == 1  # ein clipPath, keine doppelte id
    assert 'class="metric-value" data-countup' in seite
    assert "CHF" in seite.split('class="metric-value"')[1][:120]
    assert 'class="jar-pct topf-pct"' in seite      # Prozentzahl als HTML unter dem Glas
    assert 'class="jar-pct"' not in seite           # nicht mehr als SVG-<text> im Glas
    _reset_meet()
