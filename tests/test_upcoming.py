"""Fristen und erkannte Jahresposten.

Alle Buchungen hier sind erfunden und im Test selbst nachlesbar. Stichjahre weit
in der Vergangenheit halten die Auswertung vom gemeinsamen Testbestand fern.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from moneten.db.models import (
    Account,
    Category,
    ManagementType,
    ManualSubscription,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services.upcoming import (
    FRISTEN_STAND,
    SAEULE_3A_MAX,
    Posten,
    fristen,
    jahresposten,
    summe_jahresposten,
    was_kommt,
)


def _marke() -> str:
    """Zufälliges Kennzeichen NUR aus Buchstaben.

    Ziffern überleben die Händler-Normalisierung nicht (sie fliegen als
    Referenznummern raus) und wären im Anzeigenamen nicht wiederzufinden — die
    Tests suchen ihren eigenen Posten aber genau darüber.
    """
    return "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:6])


@contextmanager
def _buchungen(db, eintraege: list[tuple[date, str, str]], *, kategorie_id: int | None = None):
    """Legt Buchungen an und räumt sie ZWINGEND wieder ab."""
    konto = db.scalars(select(Account)).first()
    objekte = [
        Transaction(account_id=konto.id, date=d, amount=Decimal(betrag), description=text,
                    category_id=kategorie_id)
        for d, betrag, text in eintraege
    ]
    db.add_all(objekte)
    db.commit()
    try:
        yield objekte
    finally:
        for o in objekte:
            db.delete(o)
        db.commit()


# ---------------------------------------------------------------- Fristen


def test_fristen_liegen_immer_in_der_zukunft() -> None:
    heute = date(2026, 7, 26)
    with SessionLocal() as db:
        for p in fristen(db, heute):
            assert p.datum >= heute, f"{p.titel} liegt in der Vergangenheit"


def test_saeule_3a_nennt_den_offenen_betrag() -> None:
    """Eine Frist ohne eigene Zahl ist nur ein Kalendereintrag."""
    heute = date(2026, 7, 26)
    with SessionLocal() as db:
        p = next(f for f in fristen(db, heute) if "3a" in f.titel)
    assert str(int(SAEULE_3A_MAX)) in p.hinweis
    assert "Noch" in p.hinweis
    assert str(FRISTEN_STAND) in p.hinweis, "Das Bezugsjahr muss sichtbar dabeistehen"


def test_veraltete_konstanten_werden_gemeldet() -> None:
    """Nach dem Bezugsjahr sagt die App das offen, statt weiter zu behaupten.

    Beträge und Fristen ändern sich jährlich, und die App macht keine
    Netzwerkaufrufe — eine still veraltende Konstante wäre schlimmer als keine
    Angabe.
    """
    with SessionLocal() as db:
        frisch = was_kommt(db, date(FRISTEN_STAND, 7, 1))
        spaeter = was_kommt(db, date(FRISTEN_STAND + 1, 7, 1))
    assert frisch["veraltet"] is False
    assert spaeter["veraltet"] is True


# ---------------------------------------------------------------- Jahresposten


def test_zwei_jahre_im_selben_monat_ergeben_einen_jahresposten() -> None:
    """Autosteuer im Oktober, zwei Jahre hintereinander — die dritte kommt auch."""
    marke = _marke()
    text = f"ZZZautosteuer{marke}"
    heute = date(2013, 8, 15)
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 10, 12), "-480", text),
        (date(2012, 10, 14), "-480", text),
    ]):
        treffer = [p for p in jahresposten(db, heute) if marke in p.titel]
        assert len(treffer) == 1, f"Erwartet ein Jahresposten, bekommen {treffer}"
        assert treffer[0].datum.month == 10
        assert treffer[0].datum.year == 2013
        assert treffer[0].betrag == Decimal("480")


def test_eine_einzelne_zahlung_ist_kein_muster() -> None:
    """Sonst würde jede grössere Einmalausgabe zur Prognose."""
    marke = _marke()
    text = f"ZZZeinmalig{marke}"
    with SessionLocal() as db, _buchungen(db, [(date(2012, 10, 12), "-900", text)]):
        assert not [p for p in jahresposten(db, date(2013, 8, 15)) if marke in p.titel]


def test_bereits_gezahlt_taucht_nicht_mehr_auf() -> None:
    """Ist die Zahlung dieses Jahr schon gekommen, steht nichts mehr an."""
    marke = _marke()
    text = f"ZZZschonda{marke}"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 3, 12), "-480", text),
        (date(2012, 3, 14), "-480", text),
        (date(2013, 3, 11), "-480", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 8, 15)) if marke in p.titel]


def test_kleinbetraege_stoeren_nicht() -> None:
    """Ein wiederkehrender Zehner ist kein Termin, um den man sich kümmern muss."""
    marke = _marke()
    text = f"ZZZklein{marke}"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 10, 12), "-15", text),
        (date(2012, 10, 14), "-15", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 8, 15)) if marke in p.titel]


def test_zahlung_ausserhalb_des_horizonts_wartet() -> None:
    """Drei Monate Vorlauf reichen; alles Weitere wäre Dauerrauschen."""
    marke = _marke()
    text = f"ZZZspaet{marke}"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 10, 12), "-480", text),
        (date(2012, 10, 14), "-480", text),
    ]):
        # Im Januar ist der Oktober weit weg.
        assert not [p for p in jahresposten(db, date(2013, 1, 15)) if marke in p.titel]


# ------------------------------------------- Rechnung oder wiederholter Einkauf?
#
# Die alte Regel nahm „derselbe Händler kam in N Jahren im selben Monat" als
# Beleg. Bei rund 500 Buchungen im Jahr entstehen solche Paare zwangsläufig —
# die Karte war mit Migros-, Ochsner- und TWINT-Einkäufen zugemüllt. Die Tests
# hier decken beide Seiten ab: die Rechnung bleibt, der Einkauf fliegt raus.


def _e_banking(marke: str, name: str) -> str:
    """Buchungstext einer per E-Banking bezahlten Rechnung."""
    return f"E-Banking Auftrag an ZZZ{name}{marke} AG"


def test_jahresrechnung_bleibt_erhalten() -> None:
    """Jahresgebühr: einmal im Jahr, gleicher Betrag, kein Ladenkauf — der Kernfall.

    Ohne diesen Test könnte man die Karte auch dadurch „bereinigen", dass gar
    nichts mehr erkannt wird.
    """
    marke = _marke()
    text = _e_banking(marke, "gebuehr")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]
        assert len(treffer) == 1, f"Die Jahresrechnung fehlt: {treffer}"
        assert treffer[0].betrag == Decimal("335")
        assert treffer[0].datum == date(2013, 11, 4)


def test_wiederholter_einkauf_ist_kein_jahresposten() -> None:
    """Der gemeldete Fehler, im Original: zweimal Migros im August."""
    marke = _marke()
    text = (f"Einkauf ZZZmigros{marke} MM Musterhalle Zu 25.08.2025, 10:59, "
            "Visa Debit-Nr. 123456xxxxxx7890")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 8, 25), "-115", text),
        (date(2012, 8, 24), "-118", text),
        # Derselbe Laden quer durchs Jahr — das macht ihn zum Alltag.
        *[(date(2012, m, 6), "-87", text) for m in (2, 4, 6, 10)],
    ]):
        assert not [p for p in jahresposten(db, date(2013, 6, 15)) if marke in p.titel]


def test_zweite_zahlung_im_selben_jahr_ist_kein_jahresrhythmus() -> None:
    """Was zweimal im Jahr kommt, ist keine Jahresrechnung (Kriterium 1a)."""
    marke = _marke()
    text = _e_banking(marke, "zweimal")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
        (date(2012, 11, 20), "-335", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_zahlungen_quer_durchs_jahr_ergeben_keinen_termin() -> None:
    """Einmal im März, einmal im November — daraus folgt kein Termin (Kriterium 1b)."""
    marke = _marke()
    text = _e_banking(marke, "quer")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 3, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_rechnung_ueber_den_jahreswechsel_bleibt_erhalten() -> None:
    """28.12. und 3.1. sind derselbe Rechnungslauf, nicht elf Monate Abstand."""
    marke = _marke()
    text = _e_banking(marke, "jahreswechsel")
    with SessionLocal() as db, _buchungen(db, [
        (date(2010, 12, 28), "-335", text),
        (date(2012, 1, 3), "-335", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 11, 20)) if marke in p.titel]
        assert len(treffer) == 1, f"Der Jahreswechsel zerreisst die Reihe: {treffer}"
        assert treffer[0].datum == date(2014, 1, 3)


def test_schwankender_betrag_ist_keine_rechnung() -> None:
    """200 und 400 sind kein Betrag, der wiederkommt (Kriterium 2)."""
    marke = _marke()
    text = _e_banking(marke, "schwankt")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-200", text),
        (date(2012, 11, 4), "-400", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_kassenzahlung_ist_nie_eine_jahresrechnung() -> None:
    """Ochsner Sport: einmal im Jahr, gleicher Betrag — aber an der Kasse gezahlt.

    Isoliert Kriterium 3: alles andere spricht hier für einen Jahresposten.
    """
    marke = _marke()
    text = f"Einkauf ZZZochsner{marke} Sport / 226 Visa Debit-Nr. 123456xxxxxx7890"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 12), "-213", text),
        (date(2012, 11, 14), "-213", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_twint_zahlung_ist_nie_eine_jahresrechnung() -> None:
    """Zweiter Kassenweg aus dem Screenshot — TWINT statt Karte.

    Der Text kommt BEWUSST ohne „Einkauf" aus. Mit beiden Wörtern darin griff
    der Test über „einkauf", und „twint" durfte aus der Wortliste verschwinden,
    ohne dass ein Test rot wurde — nachgemessen und bestätigt.
    """
    marke = _marke()
    text = f"ZZZzweitshop{marke} BEISPIELSHOP TWINT"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 12), "-114", text),
        (date(2012, 11, 14), "-114", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_alltagskategorie_traegt_keine_jahresverpflichtung() -> None:
    """Was in Kost & Logis gebucht ist, ist Einkauf — egal wie stabil (Kriterium 4)."""
    marke = _marke()
    text = _e_banking(marke, "kostlogis")
    with SessionLocal() as db:
        kat = db.scalars(
            select(Category).where(Category.management_type == ManagementType.KOST_LOGIS)
        ).first()
        assert kat is not None, "Vorbedingung: eine Kost-&-Logis-Kategorie muss existieren"
        with _buchungen(db, [
            (date(2011, 11, 3), "-335", text),
            (date(2012, 11, 4), "-335", text),
        ], kategorie_id=kat.id):
            assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_hinweis_nennt_den_beleg_statt_einer_forderung() -> None:
    """„Noch nicht als Rückstellung erfasst" stand unter jedem Eintrag und wurde nie befolgt.

    Ein Satz, der immer dieselbe Handlung verlangt und nie eine Folge hat, ist
    Fülltext. An seiner Stelle steht der Beleg, aus dem die Vorhersage stammt —
    nachschlagbar und damit prüfbar.
    """
    marke = _marke()
    text = _e_banking(marke, "beleg")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-340", text),
    ]):
        p = next(p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel)
    assert "Rückstellung" not in p.hinweis, f"Die tote Forderung ist zurück: {p.hinweis}"
    assert "04.11.2012" in p.hinweis, f"Der letzte Beleg fehlt: {p.hinweis}"
    assert p.betrag == Decimal("340"), (
        f"Die Zeile muss den Betrag DIESES Belegs zeigen, zeigt aber {p.betrag}")
    assert "340" not in p.hinweis, (
        f"Derselbe Betrag steht zweimal in der Zeile: {p.hinweis} / {p.betrag}")


def test_betrag_folgt_dem_letzten_beleg_nicht_dem_median() -> None:
    """Der Median widersprach dem Beleg, der danebensteht.

    Die Zeile nennt den jüngsten Beleg als Herkunft. Der Median liegt aber
    zwischen den Jahren — die genannte Quelle und die gezeigte Zahl passten
    also nicht zusammen, und bei einer jedes Jahr steigenden Prämie war die
    Vorhersage zusätzlich systematisch zu tief.
    """
    marke = _marke()
    text = _e_banking(marke, "steigend")
    with SessionLocal() as db, _buchungen(db, [
        (date(2010, 11, 19), "-3000", text),
        (date(2011, 11, 19), "-3300", text),
        (date(2012, 11, 19), "-3600", text),
    ]):
        p = next(p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel)
    assert p.betrag == Decimal("3600"), f"Median statt letztem Beleg: {p.betrag}"


def test_steigende_praemie_faellt_nicht_mit_der_zeit_heraus() -> None:
    """Je länger die Historie, desto sicherer fiel die Prämie früher durch.

    Gegen einen festen Median über vier Jahre gemessen sprengt jede Rechnung
    irgendwann die Toleranz — Schweizer Krankenkassenprämien legen in vier
    Jahren ohne Weiteres 25 % zu. Gemessen wird deshalb von Schritt zu Schritt.
    """
    marke = _marke()
    text = _e_banking(marke, "praemie")
    with SessionLocal() as db, _buchungen(db, [
        (date(2009, 11, 19), "-2200", text),
        (date(2010, 11, 19), "-2400", text),
        (date(2011, 11, 19), "-3000", text),
        (date(2012, 11, 19), "-3100", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]
    assert len(treffer) == 1, f"Vier Jahre Historie löschen die Prämie: {treffer}"
    assert treffer[0].betrag == Decimal("3100")


def test_stetig_steigende_rechnung_reisst_nicht_am_median() -> None:
    """Was der Test darüber NICHT beweist: dass schrittweise verglichen wird.

    Bei einem einzelnen Sprung liegt auch der Median noch im Rahmen — die
    Sabotage „wieder gegen den Median" blieb dort nachgemessen grün. Erst eine
    Reihe, die jedes Jahr gleichmässig klettert, trennt beide Regeln: gegen den
    Median gemessen reisst der jüngste Beleg aus (4690 gegen 3375 + 30 %), von
    Schritt zu Schritt sind es viermal dieselben 25 %.
    """
    marke = _marke()
    text = _e_banking(marke, "klettert")
    with SessionLocal() as db, _buchungen(db, [
        (date(2009, 11, 19), "-2400", text),
        (date(2010, 11, 19), "-3000", text),
        (date(2011, 11, 19), "-3750", text),
        (date(2012, 11, 19), "-4690", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]
    assert len(treffer) == 1, f"Der Median wirft die steigende Reihe weg: {treffer}"
    assert treffer[0].betrag == Decimal("4690")


def test_mahngebuehr_zerreisst_die_jahresrechnung_nicht() -> None:
    """Ein Nebenposten desselben Händlers ist keine zweite Jahresrechnung.

    Nachgemessen: eine einzige Mahngebühr über 20 Franken liess die dreimal
    belegte Jahresrechnung komplett verschwinden — sie brach den Jahresabstand
    und den Betragsvergleich gleichzeitig.
    """
    marke = _marke()
    text = _e_banking(marke, "mahnung")
    with SessionLocal() as db, _buchungen(db, [
        (date(2010, 11, 3), "-335", text),
        (date(2011, 11, 4), "-335", text),
        (date(2012, 3, 8), "-20", text),
        (date(2012, 11, 5), "-335", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]
    assert len(treffer) == 1, f"Die Mahngebühr löscht die Rechnung: {treffer}"
    assert treffer[0].betrag == Decimal("335")


def test_halbjaehrliche_rate_ist_kein_jahresposten() -> None:
    """Zweimal im Jahr in voller Höhe ist halbjährlich, nicht jährlich.

    Die Gegenprobe zur Mahngebühr: dort wird ein KLEINER Nebenposten aussortiert,
    hier darf eine gleich grosse zweite Zahlung den Rhythmus zu Recht beenden.
    """
    marke = _marke()
    text = _e_banking(marke, "halbjahr")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 5, 3), "-335", text),
        (date(2011, 11, 3), "-335", text),
        (date(2012, 5, 4), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        assert not [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]


def test_jahreswechsel_ueberlebt_auch_vier_belege() -> None:
    """Der Fall, an dem die Kalenderjahr-Zählung nachweislich scheiterte.

    28.12. / 27.12. / 03.01. / 29.12. ist der sauberste Jahresrhythmus, den es
    gibt — aber 03.01.2013 und 29.12.2013 liegen im selben Kalenderjahr, und die
    alte Regel „höchstens eine Zahlung pro Kalenderjahr" warf die Reihe deshalb
    weg. Mit zwei Belegen konnte kein Test das sehen.
    """
    marke = _marke()
    text = _e_banking(marke, "silvester")
    with SessionLocal() as db, _buchungen(db, [
        (date(2010, 12, 28), "-410", text),
        (date(2011, 12, 27), "-410", text),
        (date(2013, 1, 3), "-410", text),
        (date(2013, 12, 29), "-410", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2014, 11, 20)) if marke in p.titel]
    assert len(treffer) == 1, f"Der Jahreswechsel zerreisst die Reihe: {treffer}"
    assert treffer[0].datum == date(2014, 12, 29), (
        "Der Termin steht auf dem Tag des letzten Belegs, nicht pauschal auf dem 28.")


def test_termin_am_monatsende_wird_nicht_pauschal_gekappt() -> None:
    """Eine Rechnung vom 31. wurde für den 28. vorhergesagt — drei Tage daneben.

    Geklemmt werden muss nur, was es im Zieljahr nicht gibt (29.02.), nicht jeder
    Tag ab dem 29.
    """
    marke = _marke()
    text = _e_banking(marke, "monatsende")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 10, 31), "-480", text),
        (date(2012, 10, 30), "-480", text),
    ]):
        p = next(p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel)
    assert p.datum == date(2013, 10, 30), f"Termin pauschal gekappt: {p.datum}"


def test_ladenkauf_wird_wortweise_geprueft() -> None:
    """„Kaufmann" ist kein „Kauf" und „Postfinance" kein „POS".

    Der Docstring von ``_ist_ladenkauf`` verspricht die Wortgrenze; ohne diesen
    Test durfte die Prüfung auf Teilstrings umgestellt werden, ohne rot zu
    werden — dann verschwände jede Rechnung mit solchen Buchstabenfolgen im
    Namen still aus der Karte.
    """
    marke = _marke()
    text = f"E-Banking Auftrag an ZZZkaufmann{marke} Postfinance Treuhand"
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        treffer = [p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel]
    assert len(treffer) == 1, (
        f"Teilstring statt Wort: Kaufmann/Postfinance gelten als Kasse: {treffer}")


def test_erfasstes_abo_verschluckt_keinen_fremden_haendler() -> None:
    """Ein gemeinsames Wort machte aus zwei Händlern einen.

    Gemessen: das Abo „Musterus Versicherung" setzte die Wortmenge
    {musterus, versicherung}, und damit verschwand die fremde Jahresprämie
    („muster versicherung praemie") aus der Karte. Verglichen wird deshalb mit
    ``key_passt`` über die Wortfolge statt über einzelne Wörter.
    """
    marke = _marke()
    text = f"E-Banking Auftrag an ZZZmuster{marke} Versicherung Praemie"
    with SessionLocal() as db:
        abo = ManualSubscription(name="Musterus Versicherung", amount=Decimal("42"),
                                 match_keyword="Musterus Versicherung", is_active=True)
        db.add(abo)
        db.commit()
        try:
            with _buchungen(db, [
                (date(2011, 11, 3), "-1290", text),
                (date(2012, 11, 4), "-1290", text),
            ]):
                treffer = [p for p in jahresposten(db, date(2013, 9, 20))
                           if marke in p.titel]
            assert len(treffer) == 1, (
                f"Ein fremdes Abo verschluckt die Prämie: {treffer}")
        finally:
            db.delete(abo)
            db.commit()


def test_erfasstes_abo_unterdrueckt_den_eigenen_haendler_weiterhin() -> None:
    """Die Gegenprobe: Was erfasst ist, darf nicht doppelt erscheinen.

    Ohne diese Seite liesse sich der Befund oben auch dadurch „beheben", dass
    gar nichts mehr unterdrückt wird.
    """
    marke = _marke()
    name = f"ZZZserafe{marke}"
    text = f"E-Banking Auftrag an {name} AG"
    with SessionLocal() as db:
        abo = ManualSubscription(name=name, amount=Decimal("28"),
                                 match_keyword=name, is_active=True)
        db.add(abo)
        db.commit()
        try:
            with _buchungen(db, [
                (date(2011, 11, 3), "-335", text),
                (date(2012, 11, 4), "-335", text),
            ]):
                assert not [p for p in jahresposten(db, date(2013, 9, 20))
                            if marke in p.titel]
        finally:
            db.delete(abo)
            db.commit()


def test_titel_nennt_den_haendler_statt_des_zahlungswegs() -> None:
    """„E-Banking Auftrag an …" stand als Vorspann in jeder Zeile.

    Auf 375 px kostet das eine zweite Zeile pro Posten und schiebt den Betrag aus
    dem Blick — für eine Angabe, die in jeder Zeile dieselbe ist.
    """
    marke = _marke()
    text = _e_banking(marke, "titel")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        p = next(p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel)
    assert "E-Banking" not in p.titel, f"Der Zahlungsweg steht wieder im Titel: {p.titel}"
    assert "Auftrag" not in p.titel, f"Der Zahlungsweg steht wieder im Titel: {p.titel}"


def test_betrag_steht_in_ganzen_franken() -> None:
    """Sonst geht die Summenzeile neben der Addition der sichtbaren Zahlen auf.

    Die Karte zeigt die Zeilenbeträge ohne Rappen (``chf_kurz``), die Summe mit
    (``chf``). Ein Beleg auf .50 stünde als „312" in der Zeile und zählte mit
    312.50 in die Summe — sichtbar falsch, und zwar genau dort, wo man nachrechnet.
    """
    marke = _marke()
    text = _e_banking(marke, "rappen")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 10, 15), "-314", text),
        (date(2012, 10, 17), "-312.50", text),
    ]):
        p = next(p for p in jahresposten(db, date(2013, 9, 20)) if marke in p.titel)
    assert p.betrag == Decimal("313"), f"Rappen in der Karte: {p.betrag}"


def test_summe_zaehlt_genau_die_gezeigten_betraege() -> None:
    """Die Summenzeile muss dieselbe Menge summieren, die darüber steht.

    Die Karte zeigt für jeden Posten mit Betrag eine Zahl; „Erwartete
    Jahreszahlungen zusammen" darf weder eine davon auslassen noch etwas
    dazuzählen, das in der Liste nicht steht.
    """
    marke = _marke()
    text = _e_banking(marke, "summe")
    with SessionLocal() as db, _buchungen(db, [
        (date(2011, 11, 3), "-335", text),
        (date(2012, 11, 4), "-335", text),
    ]):
        kommt = was_kommt(db, date(2013, 9, 20))
    gezeigt = sum((p.betrag for p in kommt["posten"] if p.betrag), Decimal("0"))
    assert gezeigt > 0, "Vorbedingung: die Liste zeigt mindestens einen Betrag"
    assert kommt["summe"] == gezeigt


def test_summe_zaehlt_keine_frist_mit() -> None:
    """Die Zeile heisst „Erwartete Jahreszahlungen zusammen" — Fristen sind keine.

    Heute tragen Fristen kein Betragsfeld, deshalb konnte der Test darüber die
    Regel nicht sehen: die Bedingung ``art == jahresposten`` durfte aus der
    Summe verschwinden, ohne rot zu werden. Hier steht der Fall direkt, denn die
    3a-Frist kennt ihren offenen Betrag längst und wird ihn irgendwann tragen.
    """
    posten = [
        Posten(datum=date(2026, 11, 4), titel="Jahresgebühr", hinweis="", art="jahresposten",
               betrag=Decimal("335")),
        Posten(datum=date(2026, 12, 31), titel="Säule 3a einzahlen", hinweis="",
               art="frist", betrag=Decimal("7258")),
    ]
    assert summe_jahresposten(posten) == Decimal("335")


def test_liste_ist_chronologisch() -> None:
    with SessionLocal() as db:
        posten = was_kommt(db, date(2026, 9, 15))["posten"]
    daten = [p.datum for p in posten]
    assert daten == sorted(daten), "Was zuerst kommt, steht zuoberst"
