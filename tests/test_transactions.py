"""Tests für die Buchungs-Erfassung und Saldo-Wirkung (Phase 1)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, Transaction
from moneten.db.session import SessionLocal

_counter = {"n": 0}


def _first_account_id() -> int:
    """Legt ein frisches, isoliertes Konto an (opening 0, keine Buchungen).

    Bewusst NICHT das geteilte Seed-Konto — die session-weite Test-DB wird von
    vielen Tests verändert, ein frisches Konto macht Saldo-Tests deterministisch.
    """
    from decimal import Decimal

    from moneten.db.models import AccountType

    _counter["n"] += 1
    with SessionLocal() as db:
        acc = Account(name=f"TX-Test {_counter['n']}", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=500)
        db.add(acc)
        db.commit()
        return acc.id


def test_transactions_page_requires_login(client: TestClient) -> None:
    resp = client.get("/transactions", follow_redirects=False)
    assert resp.status_code in (303, 307)


def test_transactions_page_loads(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/transactions")
    assert resp.status_code == 200
    assert "Buchungen" in resp.text


def test_filter_empty_account_category_no_422(logged_in_client: TestClient) -> None:
    """Regression: der HTMX-Filter sendet bei „Alle Konten/Kategorien" LEERE
    Werte (``account_id=``); diese dürfen NICHT mit 422 scheitern, sonst bleibt
    der Filter-Swap aus und der Filter wirkt tot."""
    resp = logged_in_client.get(
        "/transactions?kind=einnahme&q=&account_id=&category_id=&only_receipts=0",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_create_expense_updates_balance(logged_in_client: TestClient) -> None:
    acc_id = _first_account_id()
    with SessionLocal() as db:
        before = db.get(Account, acc_id).current_balance

    resp = logged_in_client.post(
        "/transactions",
        data={
            "kind": "ausgabe", "amount": "42.50", "date": date.today().isoformat(),
            "account_id": str(acc_id), "category_id": "", "description": "Testausgabe", "notes": "",
        },
    )
    assert resp.status_code == 200
    assert "Testausgabe" in resp.text

    with SessionLocal() as db:
        after = db.get(Account, acc_id).current_balance
    # Ausgabe verringert den Saldo um 42.50
    assert float(before) - float(after) == 42.50


def test_create_income_and_signs(logged_in_client: TestClient) -> None:
    acc_id = _first_account_id()
    logged_in_client.post(
        "/transactions",
        data={
            "kind": "einnahme", "amount": "1000", "date": date.today().isoformat(),
            "account_id": str(acc_id), "category_id": "", "description": "Lohn", "notes": "",
        },
    )
    with SessionLocal() as db:
        tx = db.scalar(select(Transaction).where(Transaction.description == "Lohn"))
        assert tx is not None
        assert float(tx.amount) == 1000.0  # Einnahme = positiv


def test_create_validation(logged_in_client: TestClient) -> None:
    acc_id = _first_account_id()
    # Betrag 0 -> Fehler
    resp = logged_in_client.post(
        "/transactions",
        data={"kind": "ausgabe", "amount": "0", "date": date.today().isoformat(),
              "account_id": str(acc_id), "category_id": "", "description": "x", "notes": ""},
    )
    assert resp.status_code == 400
    assert "grösser als 0" in resp.text

    # Ungültiger Betrag
    resp = logged_in_client.post(
        "/transactions",
        data={"kind": "ausgabe", "amount": "abc", "date": date.today().isoformat(),
              "account_id": str(acc_id), "category_id": "", "description": "x", "notes": ""},
    )
    assert resp.status_code == 400
    assert "gültige Zahl" in resp.text


def test_delete_transaction_restores_balance(logged_in_client: TestClient) -> None:
    acc_id = _first_account_id()
    with SessionLocal() as db:
        before = db.get(Account, acc_id).current_balance

    logged_in_client.post(
        "/transactions",
        data={"kind": "ausgabe", "amount": "99.00", "date": date.today().isoformat(),
              "account_id": str(acc_id), "category_id": "", "description": "Loeschtest", "notes": ""},
    )
    with SessionLocal() as db:
        tx_id = db.scalar(select(Transaction).where(Transaction.description == "Loeschtest")).id

    resp = logged_in_client.post(f"/transactions/{tx_id}/delete")
    assert resp.status_code == 200

    with SessionLocal() as db:
        after = db.get(Account, acc_id).current_balance
    # Nach dem Löschen ist der Saldo wieder wie vorher.
    assert float(before) == float(after)


def test_dashboard_renders_month_metrics(logged_in_client: TestClient) -> None:
    acc_id = _first_account_id()
    logged_in_client.post(
        "/transactions",
        data={"kind": "einnahme", "amount": "500", "date": date.today().isoformat(),
              "account_id": str(acc_id), "category_id": "", "description": "Dashboard-Eingang", "notes": ""},
    )
    resp = logged_in_client.get("/")
    assert resp.status_code == 200
    # Dashboard rendert die Monats-Metriken (Korrektheit der Summen: siehe Saldo-Tests).
    assert "Eingang" in resp.text
    assert "Ausgang" in resp.text
    assert "Saldo" in resp.text


def test_month_totals_function() -> None:
    """Isolierter Test der Monatssummen-Berechnung auf einem frischen Konto."""
    from decimal import Decimal

    from moneten.db.models import Account, AccountType, Transaction
    from moneten.routers.dashboard import _month_totals

    with SessionLocal() as db:
        acc = Account(name="Monat-Testkonto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"))
        db.add(acc)
        db.flush()
        today = date.today()
        db.add(Transaction(account_id=acc.id, date=today, amount=Decimal("200.00"), description="E"))
        db.add(Transaction(account_id=acc.id, date=today, amount=Decimal("-80.00"), description="A"))
        db.commit()

        income, expense, saldo = _month_totals(db, today)
        # income/expense summieren ALLE Konten — daher nur prüfen, dass unsere
        # Beträge enthalten sind (>=) und der Saldo = income - expense.
        assert income >= Decimal("200.00")
        assert expense >= Decimal("80.00")
        assert saldo == income - expense


def test_monthly_series_is_month_to_date() -> None:
    """Die Monatsreihe zählt nur Tag 1 bis heute: eine Buchung NACH dem heutigen
    Tag-im-Monat fällt im jeweiligen Monatsbucket weg (fairer Teilmonats-Vergleich)."""
    from decimal import Decimal

    from moneten.db.models import Account, AccountType, Transaction
    from moneten.routers.dashboard import _monthly_series

    with SessionLocal() as db:
        acc = Account(name="MTD-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"))
        db.add(acc)
        db.flush()
        # Ruhiger Vergleichsmonat (Sept 2025): je eine Buchung am 1. und am 28.
        db.add(Transaction(account_id=acc.id, date=date(2025, 9, 1), amount=Decimal("333.11"), description="früh"))
        db.add(Transaction(account_id=acc.id, date=date(2025, 9, 28), amount=Decimal("444.22"), description="spät"))
        db.commit()

        # „heute" = 15. → Sept-Bucket zählt nur bis zum 15. → die 444.22 (28.) fehlt.
        mtd = _monthly_series(db, date(2025, 11, 15), n=3)[0]["income"]
        # „heute" = 30. → ganzer Sept → 444.22 ist dabei. Differenz isoliert genau diese Buchung.
        full = _monthly_series(db, date(2025, 11, 30), n=3)[0]["income"]
        assert full - mtd == Decimal("444.22")


# ---------------------------------------------------------------------------
# Zeitraum der Liste: Vorgabe „laufendes Jahr", umschaltbar auf „alle Jahre"
# ---------------------------------------------------------------------------


def _buchung_im(jahr_versatz: int, beschreibung: str) -> int:
    """Buchung im laufenden (0) oder im vorigen (-1) Jahr, immer am 15. Juli.

    Der 15.7. liegt weit genug von beiden Jahresgrenzen entfernt, dass der Test
    nicht davon abhängt, an welchem Tag er läuft.
    """
    from decimal import Decimal

    from moneten.db.models import Transaction

    with SessionLocal() as db:
        tx = Transaction(account_id=_first_account_id(),
                         date=date(date.today().year + jahr_versatz, 7, 15),
                         amount=Decimal("-33.00"), description=beschreibung)
        db.add(tx)
        db.commit()
        return tx.id


def test_liste_zeigt_ohne_parameter_nur_das_laufende_jahr(
    logged_in_client: TestClient,
) -> None:
    """Der eigentliche Zweck: bei mehreren Jahren Buchungen öffnet die Seite
    nicht mehr mit dem Gesamtbestand.

    Geprüft wird beides — dass das Vorjahr wegbleibt UND dass es über den
    Umschalter zurückkommt. Eine Einschränkung ohne Rückweg wäre kein
    Vorgabewert, sondern eine Sperre.
    """
    _buchung_im(0, "Zeitraumprobe Aktuell Vex")
    _buchung_im(-1, "Zeitraumprobe Vorjahr Vex")

    seite = logged_in_client.get("/transactions", params={"q": "Zeitraumprobe"})
    assert seite.status_code == 200, seite.text
    assert "Zeitraumprobe Aktuell Vex" in seite.text
    assert "Zeitraumprobe Vorjahr Vex" not in seite.text, \
        "Die Seite öffnet weiterhin mit dem Gesamtbestand"

    alle = logged_in_client.get("/transactions",
                                params={"q": "Zeitraumprobe", "zeitraum": "alles"})
    assert "Zeitraumprobe Vorjahr Vex" in alle.text, \
        "Auf „alle Jahre“ umgeschaltet fehlt die ältere Buchung immer noch"


def test_umschalter_und_zahl_stehen_sichtbar_auf_der_seite(
    logged_in_client: TestClient,
) -> None:
    """Eine unsichtbare Einschränkung ist von fehlenden Daten nicht zu
    unterscheiden.

    Auf der Seite muss deshalb stehen, WELCHER Zeitraum gilt (der Umschalter mit
    markiertem Zustand) und dass es ausserhalb davon noch etwas gibt — sonst
    sucht der Nutzer einen Beleg von 2024 und hält den Import für verloren.
    """
    _buchung_im(0, "Sichtbarkeitsprobe Aktuell Wren")
    _buchung_im(-1, "Sichtbarkeitsprobe Vorjahr Wren")

    seite = logged_in_client.get("/transactions", params={"q": "Sichtbarkeitsprobe"})
    assert 'aria-label="Zeitraum der Liste"' in seite.text, "Kein Umschalter auf der Seite"
    assert re.search(rf'aria-pressed="true"[^>]*>{date.today().year}</button>', seite.text), (
        "Der Umschalter sagt nicht, welcher Zeitraum gerade gilt"
    )
    assert "Alle Jahre</button>" in seite.text, "Kein Weg zurück zum Gesamtbestand"
    assert re.search(r"1 ausserhalb", seite.text), (
        "Die Seite verschweigt, dass ausserhalb des Jahres noch Treffer liegen"
    )


def test_summe_und_liste_sprechen_immer_ueber_denselben_zeitraum(
    logged_in_client: TestClient,
) -> None:
    """Der Befund: über einer Liste, die nur 2026 zeigte, stand
    „Saldo über alle Buchungen" — die Summenzeile hatte einen eigenen Umschalter
    mit eigener Vorgabe (``sum_period=gesamt``).

    Beides hängt jetzt an EINEM Wert. Geprüft wird deshalb nicht nur die Vorgabe,
    sondern die Kopplung: in jedem der drei Zustände muss die Beschriftung der
    Leitzahl zu dem passen, was die Liste zeigt.
    """
    _buchung_im(0, "Kopplungsprobe Aktuell Sable")
    _buchung_im(-1, "Kopplungsprobe Vorjahr Sable")
    jahr = date.today().year

    vorgabe = logged_in_client.get("/transactions", params={"q": "Kopplungsprobe"})
    assert f"Saldo {jahr}" in vorgabe.text, "Die Summenzeile startet nicht im laufenden Jahr"
    assert "Saldo über alle Buchungen" not in vorgabe.text
    assert "Kopplungsprobe Vorjahr Sable" not in vorgabe.text, \
        "Die Liste zeigt mehr, als die Summe darüber behauptet"

    alles = logged_in_client.get("/transactions",
                                 params={"q": "Kopplungsprobe", "zeitraum": "alles"})
    assert "Saldo über alle Buchungen" in alles.text
    assert "Kopplungsprobe Vorjahr Sable" in alles.text, \
        "Die Summe spricht über alle Jahre, die Liste zeigt sie nicht"

    monat = logged_in_client.get("/transactions",
                                 params={"q": "Kopplungsprobe", "zeitraum": "monat"})
    assert re.search(rf"Saldo \w+ {jahr}", monat.text), \
        "Im Monats-Zustand nennt die Leitzahl den Monat nicht"
    # Die Buchung liegt am 15.7.; nur im Juli darf sie im Monats-Zustand stehen.
    if date.today().month != 7:
        assert "Kopplungsprobe Aktuell Sable" not in monat.text, \
            "Der Monats-Zustand begrenzt die Liste nicht"


def test_nur_noch_eine_zeitraum_leiste(logged_in_client: TestClient) -> None:
    """Zwei Segmentleisten kurz übereinander, beide mit einem Knopf „2026", waren
    nicht auseinanderzuhalten — und konnten verschiedene Zeiträume behaupten.

    Gezählt wird die Leiste, nicht ihr Inhalt: eine zweite, die wieder einen
    eigenen Zeitraum mitbringt, fällt hier sofort auf.
    """
    seite = logged_in_client.get("/transactions")
    # Das Leerzeichen zählt: „txs-seg-btn" sind die Knöpfe IN der Leiste.
    assert seite.text.count('class="txs-seg ') == 1, \
        "Es gibt wieder mehr als einen Zeitraum-Umschalter auf der Seite"
    assert seite.text.count('aria-label="Zeitraum') == 1, \
        "Zwei Bedienelemente behaupten, den Zeitraum zu setzen"


def test_leere_liste_nennt_das_jahr_und_die_treffer_ausserhalb(
    logged_in_client: TestClient,
) -> None:
    """„Keine Buchungen für diesen Filter" allein ist im Jahres-Modus eine Lüge.

    Die Buchung existiert, sie liegt nur im Vorjahr. Ohne diesen Zusatz sucht
    man den Beleg anschliessend im Papierarchiv statt eine Zeile höher.
    """
    _buchung_im(-1, "Leerprobe Vorjahr Quill")

    seite = logged_in_client.get("/transactions", params={"q": "Leerprobe"})
    assert "Keine Buchungen für diesen Filter" in seite.text
    assert f"in {date.today().year}" in seite.text, "Der leere Zustand nennt das Jahr nicht"
    assert "1 Treffer ausserhalb" in seite.text, \
        "Der leere Zustand verschweigt die Treffer ausserhalb des Jahres"


def test_einstiegs_leitfaden_haengt_am_leeren_bestand_nicht_am_leeren_jahr() -> None:
    """Der Leitfaden („Noch keine Buchungen." samt Einstiegs-Knöpfen) darf nur
    erscheinen, wenn es wirklich keine Buchung gibt.

    Über HTTP nicht prüfbar (die geteilte Test-DB ist nie leer), über die
    Struktur schon: der Zweig mit ``ausserhalb`` muss VOR dem Leitfaden stehen.
    Ohne ihn bekäme jeder, dessen letzte Buchung im Dezember liegt, im Januar
    den Einstiegs-Bildschirm zu sehen — als wäre sein Bestand weg.
    """
    tpl = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"
           / "partials" / "transactions_root.html").read_text(encoding="utf-8")
    assert "elif ausserhalb" in tpl, "Kein eigener Zweig für „Jahr leer, Bestand nicht“"
    assert tpl.index("elif ausserhalb") < tpl.index("empty-guide"), \
        "Der Einstiegs-Leitfaden kommt vor der Prüfung auf Buchungen anderer Jahre"


def test_nachgetragene_buchung_bleibt_nach_dem_speichern_sichtbar(
    logged_in_client: TestClient,
) -> None:
    """Speichern und nichts sehen ist von „nicht gespeichert" nicht zu unterscheiden.

    Eine Buchung mit einem Datum aus dem Vorjahr (alter Beleg nachgetragen)
    fällt aus Monatsfenster UND Jahres-Vorgabe — die Antwort muss den Zeitraum
    für diesen Fall aufmachen.
    """
    acc_id = _first_account_id()
    resp = logged_in_client.post("/transactions", data={
        "kind": "ausgabe", "amount": "19.90",
        "date": date(date.today().year - 1, 7, 15).isoformat(),
        "account_id": str(acc_id), "category_id": "",
        "description": "Nachtragsprobe Zephyr", "notes": "",
    })
    assert resp.status_code == 200, resp.text
    assert "Nachtragsprobe Zephyr" in resp.text, \
        "Die eben erfasste Buchung fehlt in der Antwort"


# ---------------------------------------------------------------------------
# Monatskopf: Zusammenfassung lesbar (beschriftet statt aufgereiht)
# ---------------------------------------------------------------------------


def _monatskopf_fluss(html: str) -> str:
    """Der Richtungs-Block der ERSTEN Monatskarte — ``mt-flow`` bis Kopfende.

    Bewusst nur dieser Ausschnitt: der Saldo trägt bei negativen Monaten selbst
    ein Minuszeichen, und die Seite zeigt weiter oben denselben Betrag im
    Summenblock. Beides würde die Prüfung auf „kein nackter Strich" verwässern.

    Der Schnitt endet deshalb am Saldo und nicht am Kopfende: ``.mt-val`` steht
    im Markup NACH ``.mt-flow``, bis ``</summary>`` lag der Saldo also mit drin.
    Gemessen mit −8'100.00 enthielt der Ausschnitt
    ``<span class="mt-val" …>−2'374.15</span>`` — der Test war nur deshalb grün,
    weil seine Daten zufällig einen positiven Saldo ergaben. Auf ein Tag-Ende zu
    schneiden hilft nicht: ``.mt-flow`` ist ein ``<span>`` voller ``<span>``.
    """
    treffer = re.search(r'class="mt-flow"(.*?)class="mt-val"', html, re.S)
    assert treffer, "Die Monatskarte hat keinen Richtungs-Block (mt-flow) vor dem Saldo"
    ausschnitt = treffer.group(1)
    # Ausdrücklich nachgeprüft statt bloss abgeschnitten: die Testdaten unten
    # ergeben zufällig positive Salden, ein zu weiter Schnitt fiele deshalb erst
    # jemandem auf, der eines Tages ein Minus erzeugt. Nachgemessen blieb der
    # Test grün, als der Schnitt wieder bis `</summary>` lief.
    assert "mt-val" not in ausschnitt, (
        f"Der Saldo liegt im Ausschnitt — der Helfer misst das falsche Element "
        f"und die Strich-Prüfung darunter ist wertlos: {ausschnitt!r}"
    )
    return ausschnitt


def test_monatskopf_beschriftet_beide_richtungen(logged_in_client: TestClient) -> None:
    """Zwei Zahlen nebeneinander sagen nicht, wovon sie handeln.

    Im Kopf stand „+5'726 · −": Einnahme, schwebender Trennpunkt, Ausgabe — und
    weil chf_kurz die Null als Gedankenstrich schreibt, blieb von der zweiten
    Zahl nur ein Strich ohne Bezugswort übrig. Genau daran ist der Nutzer
    hängengeblieben („was sind rechts die --?").

    Der Kopf muss deshalb (a) beide Richtungen mit demselben Wort benennen wie
    der Summenblock der Seite, (b) die leere Richtung als 0 zeigen statt als
    Strich und (c) ohne Trennzeichen zwischen den Beträgen auskommen.
    """
    from decimal import Decimal

    acc_id = _first_account_id()
    with SessionLocal() as db:
        # Ein Monat NUR mit Eingang: erzwingt genau den beanstandeten Fall
        # (Ausgaben = 0). Erfundene Zahl, kein echter Bestand.
        db.add(Transaction(account_id=acc_id, date=date(date.today().year, 7, 15),
                           amount=Decimal("5725.85"), description="Richtungsprobe Solmyr"))
        db.commit()

    seite = logged_in_client.get("/transactions", params={"q": "Richtungsprobe Solmyr"})
    assert seite.status_code == 200, seite.text
    # Jinja escapt den Schweizer Tausendertrenner zu &#39; — zurückgedreht, damit
    # die Erwartung so dasteht, wie der Nutzer die Zahl sieht.
    fluss = _monatskopf_fluss(seite.text).replace("&#39;", "'")

    assert ">Ein<" in fluss and ">Aus<" in fluss, \
        f"Die Beträge im Monatskopf sind unbeschriftet: {fluss!r}"
    assert ">5'726<" in fluss, f"Der Eingang des Monats fehlt: {fluss!r}"
    assert ">0<" in fluss, \
        f"Die leere Richtung steht nicht als 0 im Monatskopf: {fluss!r}"
    for strich in ("–", "—", "−"):
        assert strich not in fluss, \
            f"Ein nackter Strich statt einer Zahl steht wieder im Monatskopf: {fluss!r}"
    assert "·" not in fluss, \
        f"Der Trennpunkt zwischen den Beträgen ist zurück: {fluss!r}"


def test_monatskopf_zeigt_auch_leere_einnahmen_als_null(
        logged_in_client: TestClient) -> None:
    """Die Gegenprobe zum Test darüber — die ANDERE Richtung.

    Nachgemessen war nur die Aus-Richtung abgesichert: die Sabotage
    ``{{ g.income | chf_kurz if g.income else 0 }}`` → ``{{ g.income | chf_kurz }}``
    liess beide bestehenden Tests grün, obwohl Template- und CSS-Kommentar
    zusichern, dass BEIDE Richtungen immer stehen bleiben.
    """
    from decimal import Decimal

    acc_id = _first_account_id()
    with SessionLocal() as db:
        # Ein Monat NUR mit Ausgang. Erfundene Zahl, kein echter Bestand.
        db.add(Transaction(account_id=acc_id, date=date(date.today().year, 5, 12),
                           amount=Decimal("-431.20"), description="Gegenprobe Ylthin"))
        db.commit()

    seite = logged_in_client.get("/transactions", params={"q": "Gegenprobe Ylthin"})
    assert seite.status_code == 200, seite.text
    fluss = _monatskopf_fluss(seite.text).replace("&#39;", "'")

    assert ">431<" in fluss, f"Der Ausgang des Monats fehlt: {fluss!r}"
    assert ">0<" in fluss, \
        f"Die leere EINNAHMEN-Richtung steht nicht als 0 im Monatskopf: {fluss!r}"
    for strich in ("–", "—", "−"):
        assert strich not in fluss, \
            f"Ein nackter Strich statt einer Zahl steht im Monatskopf: {fluss!r}"


def test_monatskopf_gruppiert_ueber_den_abstand() -> None:
    """Der Abstand IST die Begründung dafür, dass kein Trennzeichen nötig ist.

    Der Kommentar in ``theme.css`` sagt: 14px zwischen den Gruppen gegen 5px
    innerhalb — „das allein bindet ‚Aus' an seinen Betrag und macht jedes
    Trennzeichen überflüssig". Ohne diesen Test überlebte ``gap: 14px → 2px``:
    die Zusage stand nur im Kommentar, die Beträge liefen zusammen, und kein
    Test wurde rot.
    """
    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")

    def luecke(klasse: str) -> int:
        regel = re.search(rf"^\.{klasse}\s*\{{(.*?)\}}", css, re.S | re.M)
        assert regel, f"Die Regel .{klasse} steht nicht mehr in theme.css"
        treffer = re.search(r"gap:\s*(\d+)px", regel.group(1))
        assert treffer, f".{klasse} hat keinen gap mehr: {regel.group(1)!r}"
        return int(treffer.group(1))

    zwischen, innerhalb = luecke("mt-flow"), luecke("mt-leg")
    assert zwischen >= 2.5 * innerhalb, (
        f"Der Abstand zwischen den Gruppen ({zwischen}px) hebt sich nicht mehr "
        f"deutlich vom Abstand innerhalb ({innerhalb}px) ab — ohne diesen "
        f"Unterschied braucht der Kopf wieder ein Trennzeichen."
    )


def test_monatstitel_bricht_nicht_um() -> None:
    """Ein umbrechender Monatstitel macht jede Karte 10px höher.

    Gemessen auf 375px: „September 2026 · 42" braucht 153px, im Kopf stehen ihm
    148px zur Verfügung. Ohne ``white-space: nowrap`` bricht er um und der Kopf
    wächst von 52 auf 62px — bei zwölf Monatskarten ein ganzer Bildschirm.
    Gekürzt (Ellipse) ist er lesbar, umgebrochen kostet er die Übersicht.
    """
    css = (Path(__file__).resolve().parents[1] / "src" / "moneten" / "static" / "css"
           / "theme.css").read_text(encoding="utf-8")
    regel = re.search(r"^\.month-title\s*\{(.*?)\}", css, re.S | re.M)
    assert regel, "Die Regel .month-title steht nicht mehr in theme.css"
    assert "white-space: nowrap" in regel.group(1), \
        f"Der Monatstitel darf nicht umbrechen: {regel.group(1)!r}"
