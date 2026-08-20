"""Tests für die Abo-Erkennung (wiederkehrende, betragsstabile Zahlungen)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import (
    Account,
    AccountType,
    Category,
    DismissedMerchant,
    ManualSubscription,
    Transaction,
)
from moneten.db.session import SessionLocal
from moneten.services.subscriptions import (
    _merchant_key,
    detect_subscriptions,
    display_name,
    key_passt,
    match_transactions,
)


def _kartentext(haendler: str, tag: date, *, zeit: str = "16:04") -> str:
    """Buchungstext im ECHTEN Format der Bank — erfundener Händler.

    Datum und maskierte Kartennummer kleben ohne Trennzeichen am Händlernamen
    („…by Anth03.07.2026, 16:04, Visa Debit-Nr. 123456xxxxxx7890"). Genau daran
    hängen Händler-Schlüssel und Anzeigename; ein Test mit sauberem Text
    („Netflix Abo") prüft das Interessante nicht.
    """
    return (f"Online Einkauf {haendler}{tag.strftime('%d.%m.%Y')}, {zeit}, "
            f"Visa Debit-Nr. 123456xxxxxx7890")


def _account() -> int:
    with SessionLocal() as db:
        acc = Account(name="Abo-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=910)
        db.add(acc)
        db.commit()
        return acc.id


def _month(today: date, back: int) -> date:
    total = (today.year * 12 + today.month - 1) - back
    return date(total // 12, total % 12 + 1, 15)


def test_detect_finds_stable_and_skips_variable() -> None:
    today = date(2026, 5, 20)
    acc = _account()
    with SessionLocal() as db:
        streaming = db.scalar(select(Category.id).where(Category.name == "Streaming"))
        # Stabil: gleiches Abo 19.90 in 4 Monaten → erkannt.
        for b in range(1, 5):
            db.add(Transaction(account_id=acc, category_id=streaming, date=_month(today, b),
                               amount=Decimal("-19.90"), description="ZZZnetflixsub Abo"))
        # Variabel: stark schwankende Beträge in 4 Monaten → kein Abo.
        for b, amt in zip((1, 2, 3, 4), ("50.00", "120.00", "30.00", "210.00"), strict=True):
            db.add(Transaction(account_id=acc, date=_month(today, b),
                               amount=Decimal("-" + amt), description="ZZZcoopvar Einkauf"))
        db.commit()

    with SessionLocal() as db:
        subs = detect_subscriptions(db, today=today)

    netflix = [s for s in subs if "zzznetflixsub" in s.name.lower()]
    assert len(netflix) == 1
    assert netflix[0].monthly == Decimal("19.90")
    assert netflix[0].months_seen == 4
    # Variabler Einkauf darf NICHT als Abo gelten.
    assert not any("zzzcoopvar" in s.name.lower() for s in subs)


def test_subscriptions_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/subscriptions")
    assert resp.status_code == 200
    assert "Abos" in resp.text


def test_manual_subscription_crud(logged_in_client: TestClient) -> None:
    """Manuelles Abo anlegen → bearbeiten → löschen."""
    r = logged_in_client.post("/subscriptions",
                              data={"name": "ZZZ-Spotify", "amount": "12.95", "interval": "monatlich"})
    assert r.status_code == 200 and "ZZZ-Spotify" in r.text
    with SessionLocal() as db:
        m = db.scalar(select(ManualSubscription).where(ManualSubscription.name == "ZZZ-Spotify"))
        assert m is not None and m.amount == Decimal("12.95")
        mid = m.id

    r = logged_in_client.post(f"/subscriptions/{mid}/update",
                              data={"name": "ZZZ-Spotify Family", "amount": "17.95", "interval": "monatlich"})
    assert r.status_code == 200 and "ZZZ-Spotify Family" in r.text
    with SessionLocal() as db:
        assert db.get(ManualSubscription, mid).amount == Decimal("17.95")

    logged_in_client.post(f"/subscriptions/{mid}/delete")
    with SessionLocal() as db:
        assert db.get(ManualSubscription, mid) is None


def test_kind_split_abo_vs_fix(logged_in_client: TestClient) -> None:
    """Typ-Umschalter: 'fix' landet als wiederkehrende Zahlung, 'abo' als Abo."""
    r = logged_in_client.post(
        "/subscriptions",
        data={"name": "ZZZ-Miete", "amount": "1500", "interval": "monatlich", "kind": "fix"},
    )
    assert r.status_code == 200
    assert "Wiederkehrende Zahlungen" in r.text   # eigene Sektion vorhanden
    assert "ZZZ-Miete" in r.text
    with SessionLocal() as db:
        m = db.scalar(select(ManualSubscription).where(ManualSubscription.name == "ZZZ-Miete"))
        assert m is not None and m.kind == "fix"
        db.delete(m)
        db.commit()

    logged_in_client.post(
        "/subscriptions",
        data={"name": "ZZZ-AboX", "amount": "9.90", "interval": "monatlich", "kind": "abo"},
    )
    with SessionLocal() as db:
        m = db.scalar(select(ManualSubscription).where(ManualSubscription.name == "ZZZ-AboX"))
        assert m is not None and m.kind == "abo"
        db.delete(m)
        db.commit()


def test_detect_excludes_dismissed() -> None:
    """Ein als „kein Abo" markierter Händler wird nicht mehr erkannt."""
    today = date(2026, 5, 20)
    acc = _account()
    with SessionLocal() as db:
        for b in range(1, 5):
            db.add(Transaction(account_id=acc, date=_month(today, b), amount=Decimal("-33.00"),
                               description="ZZZdismdetect Abo"))
        db.commit()
    key = _merchant_key("ZZZdismdetect Abo")
    with SessionLocal() as db:
        assert any(s.key == key for s in detect_subscriptions(db, today=today))
        db.add(DismissedMerchant(merchant_key=key))
        db.commit()
    with SessionLocal() as db:
        assert not any(s.key == key for s in detect_subscriptions(db, today=today))
        d = db.scalar(select(DismissedMerchant).where(DismissedMerchant.merchant_key == key))
        if d:
            db.delete(d)
            db.commit()


def test_detect_marks_stale() -> None:
    """Länger keine Zahlung → stale=True (vermutlich gekündigt); laufendes Abo nicht."""
    today = date(2026, 5, 20)
    acc = _account()
    with SessionLocal() as db:
        for b in (4, 5, 6):  # letzte Zahlung 4 Monate her
            db.add(Transaction(account_id=acc, date=_month(today, b), amount=Decimal("-13.00"),
                               description="ZZZstalesub Abo"))
        for b in (0, 1, 2):  # läuft noch
            db.add(Transaction(account_id=acc, date=_month(today, b), amount=Decimal("-9.90"),
                               description="ZZZactivesub Abo"))
        db.commit()
    with SessionLocal() as db:
        subs = detect_subscriptions(db, today=today)
    stale = [s for s in subs if "zzzstalesub" in s.name.lower()]
    active = [s for s in subs if "zzzactivesub" in s.name.lower()]
    assert len(stale) == 1 and stale[0].stale is True and stale[0].months_since >= 3
    assert len(active) == 1 and active[0].stale is False


def test_match_transactions_and_skip() -> None:
    """match_transactions findet Buchungen + kanonischen Schlüssel; extra_skip blendet ihn aus."""
    today = date.today()
    acc = _account()
    with SessionLocal() as db:
        for b in (0, 1, 2):
            db.add(Transaction(account_id=acc, date=_month(today, b), amount=Decimal("-13.00"),
                               description="TWINT ZZZSPOTI monatlich"))
        db.commit()
    with SessionLocal() as db:
        m = match_transactions(db, "zzzspoti")
        assert m is not None
        assert "zzzspoti" in m["keyword"]
        assert m["count"] == 3
        assert m["monthly"] == Decimal("13.00")
        key = m["keyword"]
        assert any(s.key == key for s in detect_subscriptions(db))
        assert not any(s.key == key for s in detect_subscriptions(db, extra_skip={key}))


def test_restore_dismissed_route(logged_in_client: TestClient) -> None:
    with SessionLocal() as db:
        db.add(DismissedMerchant(merchant_key="zzzrestoreme"))
        db.commit()
    r = logged_in_client.post("/subscriptions/restore", data={"merchant_key": "zzzrestoreme"})
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(DismissedMerchant).where(DismissedMerchant.merchant_key == "zzzrestoreme")) is None


def test_match_search_prefills_form(logged_in_client: TestClient) -> None:
    today = date.today()
    acc = _account()
    with SessionLocal() as db:
        for b in (0, 1, 2):
            db.add(Transaction(account_id=acc, date=_month(today, b), amount=Decimal("-7.00"),
                               description="ZZZMATCHQ Dienst"))
        db.commit()
    r = logged_in_client.get("/subscriptions?match_q=zzzmatchq")
    assert r.status_code == 200
    assert "3 Buchungen" in r.text
    assert 'name="match_keyword"' in r.text


def test_manual_match_keyword_stored(logged_in_client: TestClient) -> None:
    r = logged_in_client.post("/subscriptions", data={
        "name": "ZZZ-Connected", "amount": "10.00", "interval": "monatlich", "match_keyword": "zzzconn key",
    })
    assert r.status_code == 200
    with SessionLocal() as db:
        m = db.scalar(select(ManualSubscription).where(ManualSubscription.name == "ZZZ-Connected"))
        assert m is not None and m.match_keyword == "zzzconn key"
        db.delete(m)
        db.commit()


def test_adopt_detected_creates_manual(logged_in_client: TestClient) -> None:
    """„Übernehmen" macht aus einem erkannten Abo ein manuelles — VERBUNDEN, nicht ausgeblendet.

    Vorher landete der Händler zusätzlich in den ausgeblendeten: der übernommene
    Eintrag zeigte danach „0 verbundene Buchungen", und die Liste der bewusst
    ausgeblendeten Händler füllte sich mit Einträgen, die niemand ausgeblendet
    hatte. Das Doppelzählen verhindert seither ``match_keyword``.
    """
    r = logged_in_client.post("/subscriptions/adopt",
                              data={"merchant_key": "zzzadoptkey", "name": "ZZZ-Adopt", "amount": "9.90", "category_id": ""})
    assert r.status_code == 200
    with SessionLocal() as db:
        m = db.scalar(select(ManualSubscription).where(ManualSubscription.name == "ZZZ-Adopt"))
        assert m is not None
        assert m.match_keyword == "zzzadoptkey"
        assert db.scalar(select(DismissedMerchant).where(DismissedMerchant.merchant_key == "zzzadoptkey")) is None


# ---------------------------------------------------------------------------
# Erkennung mit echten Buchungstexten: Preiswechsel, Rhythmus, Schlüssel
# ---------------------------------------------------------------------------


def test_stufenwechsel_kippt_die_erkennung_nicht() -> None:
    """Ein Abo, dessen Preis von 20 auf 100 springt, bleibt ein Abo.

    Regressionstest: die Erkennung verlangte einen Variationskoeffizienten
    ≤ 0.25 über das GANZE Fenster. Nach dem Wechsel auf ein höheres Abo lag er
    bei 0.67, und das Abo verschwand von der Seite — obwohl gerade der Wechsel
    die Information ist, die man sehen will.
    """
    today = date(2026, 8, 5)
    acc = _account()
    with SessionLocal() as db:
        for i, betrag in enumerate(["20.00"] * 4 + ["100.00"] * 4):
            tag = date(2026, 1 + i, 3)
            db.add(Transaction(account_id=acc, date=tag, amount=-Decimal(betrag),
                               description=_kartentext("Zahldienst Zzzmusterabo by Muster", tag)))
        db.commit()

    with SessionLocal() as db:
        treffer = [s for s in detect_subscriptions(db, today=today) if "zzzmusterabo" in s.key]

    assert len(treffer) == 1, "Der Preiswechsel darf die Gruppe nicht verwerfen"
    s = treffer[0]
    assert s.amount == Decimal("100.00"), "Gerechnet wird mit dem AKTUELLEN Preis"
    assert s.monthly == Decimal("100.00")
    assert s.vorher == Decimal("20.00"), "Der frühere Preis ist die Nachricht, nicht Ballast"
    assert s.seit == date(2026, 5, 3)
    assert s.zahlungen == 8
    # Der Median über alles wäre 60.00 gewesen — ein Preis, den es nie gab.
    assert s.monthly != Decimal("60.00")


def test_anzeigename_enthaelt_kein_datum_und_keine_kartennummer() -> None:
    """Der rohe Buchungstext ist als Name unbrauchbar.

    Er stand bisher vollständig in der erkannten Zeile und im Namensfeld des
    Formulars — samt Uhrzeit und maskierter Kartennummer.
    """
    roh = _kartentext("Zahldienst Zzzmusterabo by Muster", date(2026, 7, 3))
    name = display_name(roh)
    assert name == "Zahldienst Zzzmusterabo"
    for verboten in ("2026", "Visa", "123456", "xxxxxx", ","):
        assert verboten not in name, f"{verboten!r} gehört nicht in einen Namen: {name!r}"


def test_wocheneinkauf_ist_kein_abo() -> None:
    """Gleicher Betrag, aber alle vier Tage — das ist kein Abo-Rhythmus.

    Vorher wurde über MONATSSUMMEN gemittelt: sieben bis acht gleich teure
    Kaffees pro Monat ergaben eine fast konstante Monatssumme und damit einen
    Variationskoeffizienten unter der Schranke. Der Kiosk stand als Abo da.
    """
    today = date(2026, 8, 5)
    acc = _account()
    with SessionLocal() as db:
        tag = date(2026, 4, 1)
        while tag <= date(2026, 7, 31):   # vier volle Monate, damit min_months greift
            db.add(Transaction(account_id=acc, date=tag, amount=Decimal("-12.50"),
                               description=_kartentext("Zzzkiosk Bahnhof ", tag)))
            tag += timedelta(days=4)
        db.commit()

    with SessionLocal() as db:
        subs = detect_subscriptions(db, today=today)

    assert not any("zzzkiosk" in s.key for s in subs)


def test_vierteljaehrlich_zaehlt_als_drittel_pro_monat() -> None:
    """Eine Quartalsrechnung darf nicht als Monatsbetrag durchgehen.

    Vorher war ``monthly`` der Median der Monatssummen — bei einer
    Vierteljahresrechnung also der volle Betrag, dreimal zu hoch. Und „vor
    3 Monaten zuletzt gezahlt" machte sie zusätzlich fälschlich zu „beendet".
    """
    today = date(2026, 8, 5)
    acc = _account()
    with SessionLocal() as db:
        for i in range(4):
            tag = date(2025, 8, 15) + timedelta(days=91 * i)   # letzte Zahlung ~3 Mt her
            db.add(Transaction(account_id=acc, date=tag, amount=Decimal("-216.00"),
                               description=_kartentext("Zzzstromwerk Musterstadt ", tag)))
        db.commit()

    with SessionLocal() as db:
        treffer = [s for s in detect_subscriptions(db, today=today) if "zzzstromwerk" in s.key]

    assert len(treffer) == 1
    s = treffer[0]
    assert s.rhythmus == "vierteljährlich"
    assert s.amount == Decimal("216.00")
    assert s.monthly == Decimal("72.00")
    assert s.stale is False, "Der Rhythmus bestimmt, ab wann eine Zahlung fehlt"


def test_verschieden_lange_buchungstexte_zaehlen_zusammen() -> None:
    """Zwei Schreibweisen desselben Händlers ergaben zwei zu kleine Gruppen.

    „Zzzsalt Mobile SA" und „Zzzsalt Mobile SA Abo" liefern die Schlüssel
    „zzzsalt mobile" und „zzzsalt mobile abo". Mit je zwei Zahlungen fiel vorher
    BEIDE Gruppen durch die Mindestanzahl — das Handy-Abo fehlte auf der Seite.
    """
    today = date(2026, 8, 5)
    acc = _account()
    with SessionLocal() as db:
        for i in range(4):
            tag = date(2026, 4 + i, 4)
            haendler = "Zzzsalt Mobile SA " if i < 2 else "Zzzsalt Mobile SA Abo"
            db.add(Transaction(account_id=acc, date=tag, amount=Decimal("-29.95"),
                               description=_kartentext(haendler, tag)))
        db.commit()

    with SessionLocal() as db:
        treffer = [s for s in detect_subscriptions(db, today=today) if "zzzsalt" in s.key]

    assert len(treffer) == 1, f"Ein Händler, eine Zeile — bekommen: {[t.key for t in treffer]}"
    assert treffer[0].zahlungen == 4
    assert treffer[0].monthly == Decimal("29.95")


def test_alter_gespeicherter_schluessel_trifft_zusammengezogene_gruppe() -> None:
    """Bestehende Verknüpfungen dürfen durch das Zusammenziehen nicht ins Leere laufen.

    ``DismissedMerchant.merchant_key`` und ``ManualSubscription.match_keyword``
    stammen aus einem einzelnen früheren Buchungstext. Wird die Gruppe unter dem
    längeren Schlüssel geführt, muss der kürzere gespeicherte sie weiter treffen.
    """
    assert key_passt("zzzsalt mobile", "zzzsalt mobile abo")
    assert key_passt("zzzsalt mobile abo", "zzzsalt mobile")
    assert not key_passt("zzzsalt home", "zzzsalt mobile")

    today = date(2026, 8, 5)
    acc = _account()
    with SessionLocal() as db:
        for i in range(4):
            tag = date(2026, 4 + i, 6)
            haendler = "Zzzdismalt Mobile SA " if i < 2 else "Zzzdismalt Mobile SA Abo"
            db.add(Transaction(account_id=acc, date=tag, amount=Decimal("-19.95"),
                               description=_kartentext(haendler, tag)))
        db.commit()

    with SessionLocal() as db:
        assert any("zzzdismalt" in s.key for s in detect_subscriptions(db, today=today))
        db.add(DismissedMerchant(merchant_key="zzzdismalt mobile"))  # kurzer Alt-Schlüssel
        db.commit()
    try:
        with SessionLocal() as db:
            assert not any("zzzdismalt" in s.key for s in detect_subscriptions(db, today=today))
    finally:
        with SessionLocal() as db:
            d = db.scalar(select(DismissedMerchant).where(
                DismissedMerchant.merchant_key == "zzzdismalt mobile"))
            if d:
                db.delete(d)
                db.commit()


def test_suchvorschau_zeigt_einzelne_treffer_und_aktuellen_betrag() -> None:
    """Die Vorschau meldete nur „8 Buchung(en) … Ø CHF 20.00/Mt".

    Bei einem Preiswechsel beschreibt dieser Durchschnitt einen Preis, den es
    nie gab, und man sieht nicht, WELCHE Buchungen gemeint sind.
    """
    today = date.today()
    acc = _account()
    with SessionLocal() as db:
        for b, betrag in zip(range(6, -1, -1), ["20.00"] * 4 + ["100.00"] * 3, strict=True):
            tag = _month(today, b)
            db.add(Transaction(account_id=acc, date=tag, amount=-Decimal(betrag),
                               description=_kartentext("Zzzsuchabo Dienst by Anth", tag)))
        db.commit()

    with SessionLocal() as db:
        m = match_transactions(db, "zzzsuchabo")

    assert m is not None
    assert m["count"] == 7
    assert m["amount"] == Decimal("100.00"), "Aktueller Betrag, kein Mittel über die Vergangenheit"
    assert m["vorher"] == Decimal("20.00")
    assert m["seit"] == _month(today, 2)
    assert m["name"] == "Zzzsuchabo Dienst", "Der Rohtext taugt nicht als Vorschlag"
    assert len(m["hits"]) == 7, "Ohne die Einzeltreffer sagt die Vorschau nichts"
    assert m["hits"][0]["date"] == _month(today, 0), "Neueste zuerst"
    assert m["hits"][0]["amount"] == Decimal("100.00")


def test_suchvorschau_im_html(logged_in_client: TestClient) -> None:
    """Derselbe Nachweis im ausgelieferten HTML: Treffer, aktueller Betrag, sauberer Name."""
    today = date.today()
    acc = _account()
    with SessionLocal() as db:
        for b, betrag in zip(range(4, -1, -1), ["15.00", "15.00", "15.00", "44.00", "44.00"], strict=True):
            tag = _month(today, b)
            db.add(Transaction(account_id=acc, date=tag, amount=-Decimal(betrag),
                               description=_kartentext("Zzzhtmlabo Service by Anth", tag)))
        db.commit()

    r = logged_in_client.get("/subscriptions?match_q=zzzhtmlabo")
    assert r.status_code == 200
    assert "5 Buchungen" in r.text
    assert _month(today, 0).strftime("%d.%m.%Y") in r.text, "Konkrete Treffer mit Datum"
    assert "CHF 44.00" in r.text, "Aktueller Betrag"
    assert "CHF 15.00" in r.text, "Früherer Betrag als Änderung"
    assert 'value="Zzzhtmlabo Service"' in r.text, "Sauberer Namensvorschlag im Formular"
    # Kein Feld darf den Rohtext tragen — im Screenshot des Nutzers stand der
    # komplette Buchungstext samt Kartennummer im Namensfeld.
    import re
    assert not any("123456" in w for w in re.findall(r'value="([^"]*)"', r.text)), \
        "Kartennummer darf in keinem Eingabefeld stehen"


@contextmanager
def _kuendigungs_szenario(db, marke: str, *, saldo="1000", ziel_betrag="1600",
                          waechst=True, abo="100"):
    """Ein Sparziel mit exakt bekannter Sparrate — damit der Zeitgewinn nachrechenbar ist.

    Voreinstellung: Konto 1'000, waechst um 100 im Monat, Ziel 1'600, also 600
    offen, Abo 100 im Monat. Damit heute 600/100 = 6 Monate, nach der Kuendigung
    600/200 = 3 Monate, Gewinn 3 Monate = 13 Wochen (3 * 4.345 = 13.035).

    Feste historische Daten statt ``date.today()``: sonst haengt der Sollwert vom
    Kalender ab, und ein wandernder Sollwert laesst keine scharfe Zahl zu.

    Raeumt am Ende ZWINGEND auf — auch wenn der Test scheitert. ``_naechstes_ziel``
    waehlt das Ziel mit dem kleinsten Rest ueber ALLE Ziele der Datenbank;
    liegengelassene Testziele entscheiden sonst den naechsten Test mit.
    """
    from datetime import date
    from decimal import Decimal

    from moneten.db.models import (
        Account,
        AccountType,
        BudgetInterval,
        ManualSubscription,
        SavingsGoal,
        Transaction,
    )

    konto = Account(name=f"ZZZspar{marke}", type=AccountType.SAVINGS, currency="CHF",
                    opening_balance=Decimal("0"), current_balance=Decimal(saldo),
                    sort_order=990)
    db.add(konto)
    db.flush()
    buchungen = []
    if waechst:
        for monat in range(2, 8):  # Februar bis Juli: sechs Monatsenden, +100 je Monat
            t = Transaction(account_id=konto.id, date=date(2015, monat, 10),
                            amount=Decimal("100"), description=f"ZZZsparen{marke}")
            buchungen.append(t)
            db.add(t)
    ziel = SavingsGoal(name=f"ZZZziel{marke}", target_amount=Decimal(ziel_betrag),
                       account_id=konto.id)
    sub = ManualSubscription(name=f"ZZZabo{marke}", amount=Decimal(abo),
                             interval=BudgetInterval.MONATLICH, kind="abo")
    db.add_all([ziel, sub])
    db.commit()
    try:
        yield konto, ziel, sub
    finally:
        for obj in (*buchungen, ziel, sub, konto):
            db.delete(obj)
        db.commit()


def test_kuendigung_rechnet_zeitgewinn_nicht_gesamtdauer(logged_in_client: TestClient) -> None:
    """„Wie viel frueher", nicht „wie lange insgesamt".

    Regressionstest: Der erste Entwurf teilte den Restbetrag durch die Abo-Rate
    und meldete bei einem 22-Franken-Abo „789 Wochen frueher" — also 15 Jahre.

    Die erste Fassung DIESES Tests war wertlos: die Pruefung stand hinter
    ``if eff["ziel"] and eff["ziel"]["wochen"]:`` und lief nie, wenn der
    Zielbezug ganz wegfiel. Und die Schranke „hoechstens die Restdauer" war so
    weit, dass der urspruengliche Fehler (26 statt 13 Wochen) durchpasste.
    Deshalb jetzt ein exakter Sollwert ohne Vorbedingung.
    """
    import uuid
    from datetime import date
    from decimal import Decimal

    from moneten.db.session import SessionLocal
    from moneten.services.cancel_effect import kuendigungs_effekt

    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db, _kuendigungs_szenario(db, marke) as (_k, _z, sub):
        eff = kuendigungs_effekt(db, sub, date(2015, 7, 20))

    assert eff is not None
    assert eff["jaehrlich"] == Decimal("1200.00")
    assert eff["ziel"] is not None, "Der Zielbezug ist der Sinn der Rechnung"
    assert eff["ziel"]["rest"] == Decimal("600"), "1600 Ziel minus 1000 angespart"
    assert eff["ziel"]["wochen"] == 13, (
        f"6 Monate ohne, 3 Monate mit Kuendigung → 3 * 4.345 = 13 Wochen Gewinn, "
        f"bekommen: {eff['ziel']['wochen']}"
    )


def test_zeitgewinn_bleibt_unter_der_restdauer(logged_in_client: TestClient) -> None:
    """Die Eigenschaft hinter dem Fehler: frueher fertig heisst nicht sofort fertig.

    Selbst wenn die Rechnung einmal anders aufgebaut wird, muss das gelten —
    ein Gewinn groesser als die verbleibende Dauer ist immer Unsinn.
    """
    import uuid
    from datetime import date

    from moneten.db.session import SessionLocal
    from moneten.services.cancel_effect import _sparrate, kuendigungs_effekt

    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db, _kuendigungs_szenario(db, marke) as (konto, _z, sub):
        heute = date(2015, 7, 20)
        rate = _sparrate(db, konto, heute)
        eff = kuendigungs_effekt(db, sub, heute)

    assert rate > 0
    restdauer_wochen = float(eff["ziel"]["rest"] / rate) * 4.345
    assert 0 < eff["ziel"]["wochen"] < restdauer_wochen, (
        f"Gewinn {eff['ziel']['wochen']} Wochen bei nur {restdauer_wochen:.1f} Wochen Restdauer"
    )


def test_ohne_sparfortschritt_steht_ein_hinweis_statt_einer_zahl(
    logged_in_client: TestClient,
) -> None:
    """Waechst das Zielkonto nicht, gibt es kein „frueher" — dann keine erfundene Zahl."""
    import uuid
    from datetime import date

    from moneten.db.session import SessionLocal
    from moneten.services.cancel_effect import kuendigungs_effekt

    marke = uuid.uuid4().hex[:4]
    with SessionLocal() as db, _kuendigungs_szenario(
        db, marke, saldo="500", ziel_betrag="2000", waechst=False, abo="30"
    ) as (_k, _z, sub):
        eff = kuendigungs_effekt(db, sub, date(2015, 7, 20))

        assert eff["ziel"] is not None
        assert eff["ziel"]["wochen"] is None
        assert eff["ziel"]["hinweis"], "Ohne Zahl muss dort ein erklaerender Satz stehen"


def test_kuendigung_nur_fuer_abos_nicht_fuer_fixkosten(logged_in_client: TestClient) -> None:
    """„Miete kündigen spart 17'400 im Jahr" ist kein sinnvoller Hinweis."""
    from decimal import Decimal

    from moneten.db.models import BudgetInterval, ManualSubscription
    from moneten.db.session import SessionLocal
    from moneten.services.cancel_effect import kuendigungs_effekt

    with SessionLocal() as db:
        fix = ManualSubscription(name="ZZZ-Miete", amount=Decimal("1450"),
                                 interval=BudgetInterval.MONATLICH, kind="fix")
        assert kuendigungs_effekt(db, fix) is None
