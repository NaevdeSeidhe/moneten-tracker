"""Tests für die Härtungen aus dem Security-Audit.

Deckt ab: Sicherheits-Header/CSP, Login-Drossel, NaN/Inf-Schutz im
Betragsparser, XXE/DTD-Ablehnung im CAMT-Parser und die Äquivalenz der
gebündelten Budget-Query.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import pytest
from fastapi.testclient import TestClient

from moneten.money import parse_amount
from moneten.services.camt053_parser import parse_camt053


# ----------  Sicherheits-Header / CSP  ----------
def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


# ----------  Login-Brute-Force-Drossel  ----------
def test_login_throttle_helpers() -> None:
    from moneten.routers.auth_pin import (
        _FAIL_MAX,
        _clear_failures,
        _record_failure,
        _too_many_attempts,
    )

    _clear_failures()
    try:
        assert _too_many_attempts("1.2.3.4") is False
        for _ in range(_FAIL_MAX):
            _record_failure("1.2.3.4")
        assert _too_many_attempts("1.2.3.4") is True
        # Und die entscheidende Haelfte: ein ANDERER Absender ist davon nicht
        # betroffen. Vorher war die Drossel global — wer den Port erreichte,
        # sperrte damit den Betreiber aus, und das dauerhaft.
        assert _too_many_attempts("5.6.7.8") is False
    finally:
        _clear_failures()  # globalen Zustand für andere Tests wieder freigeben


# ----------  parse_amount: NaN/Infinity ablehnen  ----------
def test_parse_amount_rejects_non_finite() -> None:
    for bad in ["nan", "NaN", "inf", "Infinity", "-inf"]:
        with pytest.raises(InvalidOperation):
            parse_amount(bad)
    # Gültige Eingaben weiterhin korrekt.
    assert parse_amount("1'234.50") == Decimal("1234.50")
    assert parse_amount("1234,50") == Decimal("1234.50")
    assert parse_amount("") == Decimal("0")


# ----------  CAMT-Parser: DTD/Entities ablehnen (billion laughs)  ----------
def test_camt_rejects_dtd_entities() -> None:
    payload = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE lolz [<!ENTITY lol "lol">]>\n'
        b"<Document><BkToCstmrStmt/></Document>"
    )
    with pytest.raises(ValueError):
        parse_camt053(payload)


# ----------  Budget: gebündelte Query == Einzelabfrage  ----------
def test_ist_map_matches_single_query() -> None:
    from sqlalchemy import select

    from moneten.dates import add_months
    from moneten.db.models import Account, AccountType, Category, Transaction
    from moneten.db.session import SessionLocal
    from moneten.services.median_budget import ist_for_category, ist_map

    m = date.today().replace(day=1)
    with SessionLocal() as db:
        acc = Account(name="ISTmap-Konto", type=AccountType.BANK, currency="CHF",
                      opening_balance=Decimal("0"), current_balance=Decimal("0"), sort_order=800)
        db.add(acc)
        db.commit()
        # "Velo" wird von keinem anderen Test berührt → isolierte Ist-Werte.
        velo = db.scalar(select(Category).where(Category.name == "Velo"))
        db.add(Transaction(account_id=acc.id, category_id=velo.id, date=m, amount=Decimal("-30.00"), description="A"))
        db.add(Transaction(account_id=acc.id, category_id=velo.id, date=m, amount=Decimal("-12.50"), description="B"))
        db.commit()

        imap = ist_map(db, add_months(m, -6), add_months(m, 1))
        single = ist_for_category(db, velo.id, m)

    assert single == Decimal("42.50")
    assert imap.get((velo.id, m), Decimal("0")) == single


# ----------  Session-Cookie: die Flags, nicht nur der Inhalt  ----------
def test_session_cookie_traegt_die_schutz_flags(logged_in_client: TestClient) -> None:
    """HttpOnly, SameSite und Path muessen am Cookie stehen.

    Ohne ``HttpOnly`` liest jedes eingeschleuste Skript die Sitzung aus; ohne
    ``SameSite`` schickt sie der Browser auch bei einem Klick von fremder Seite
    mit. Beides steht heute richtig da — und genau deshalb gibt es diesen Test:
    eine Zeile, die stillschweigend wegfaellt, faellt sonst niemandem auf.

    ``Secure`` wird hier NICHT geprueft. Im Test laeuft die App mit
    ``MONETEN_DEV_MODE=true`` ueber http, und dort waere ein ``Secure``-Cookie
    eines, das der Browser nie zuruecksendet. Der Schalter dafuer steht in
    ``issue_session`` an einer Stelle: ``secure=not settings.dev_mode``.
    """
    antwort = logged_in_client.post("/login", data={"pin": "424242"},
                                    follow_redirects=False)
    roh = antwort.headers.get("set-cookie", "")
    assert "HttpOnly" in roh, f"HttpOnly fehlt am Session-Cookie: {roh}"
    assert "SameSite=lax" in roh.replace("samesite", "SameSite"), roh
    assert "Path=/" in roh, roh


# ----------  Drossel: die Antwort, nicht nur die Hilfsfunktion  ----------
def test_login_drossel_antwortet_mit_429(client: TestClient) -> None:
    """Nach genug Fehlversuchen antwortet die Route selbst mit 429.

    Die Hilfsfunktionen einzeln zu pruefen genuegt nicht: sie koennten
    tadellos rechnen und trotzdem nirgends aufgerufen werden. Gemessen wird
    deshalb, was ein Angreifer sieht.
    """
    from moneten.routers.auth_pin import _FAIL_MAX, _clear_failures

    _clear_failures()
    try:
        for _ in range(_FAIL_MAX):
            client.post("/login", data={"pin": "000000"}, follow_redirects=False)
        gesperrt = client.post("/login", data={"pin": "000000"}, follow_redirects=False)
        assert gesperrt.status_code == 429
        # Auch die RICHTIGE PIN kommt jetzt nicht durch — sonst waere die
        # Drossel nur eine Verzoegerung fuer den, der schon danebenlag.
        richtig = client.post("/login", data={"pin": "424242"}, follow_redirects=False)
        assert richtig.status_code == 429
    finally:
        _clear_failures()


# ----------  Beleg-Foto: Pfad aus dem Formular ist ein Vorschlag  ----------
def test_fotopfad_ausserhalb_des_ordners_wird_verworfen(tmp_path) -> None:
    """Ein Pfad aus dem Browser darf nicht ungeprueft in die Datenbank.

    Heute wird der Wert nur gespeichert. Wer ihn spaeter anzeigen will, sieht
    dem Feld nicht mehr an, dass sein Inhalt von aussen kam — deshalb wird er
    beim Hereinkommen geprueft und nicht beim Hinausgehen.
    """
    from moneten.routers.import_bank import _foto_ordner, _geprueftes_fotoziel

    fremd = tmp_path / "geheim.jpg"
    fremd.write_bytes(b"kein Beleg")
    assert _geprueftes_fotoziel(str(fremd)) is None
    assert _geprueftes_fotoziel("../../etc/passwd") is None
    assert _geprueftes_fotoziel("") is None

    ordner = _foto_ordner()
    ordner.mkdir(parents=True, exist_ok=True)
    echt = ordner / "probe.jpg"
    echt.write_bytes(b"Beleg")
    try:
        assert _geprueftes_fotoziel(str(echt)) == str(echt.resolve())
    finally:
        echt.unlink()


def test_hsts_nur_ausserhalb_des_entwicklungsmodus(client, monkeypatch) -> None:
    """Ohne HSTS genügt ein einziger http-Aufruf, um die Sitzung offen zu zeigen.

    Und im Entwicklungsmodus darf er NICHT gesetzt werden: dort läuft die App
    über http, und der Header nagelte den Browser für Monate auf https fest —
    auf localhost eine Sackgasse, die man nur über die Browser-Einstellungen
    wieder loswird.
    """
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    kopf = client.get("/login").headers
    assert "max-age=" in kopf.get("strict-transport-security", ""), dict(kopf)

    monkeypatch.setattr(settings, "dev_mode", True)
    assert "strict-transport-security" not in {k.lower() for k in client.get("/login").headers}


def test_suchbegriffe_stehen_nicht_im_zugriffsprotokoll() -> None:
    """Die Datenbank ist verschlüsselt — das Protokoll daneben war es nie.

    Wer nach „Zahnarzt" sucht, schrieb das Frueher als Klartext-Zeile
    in eine Datei, die in jede System-Sicherung wandert. Eine Suchhistorie sagt
    oft mehr aus als der einzelne Datensatz, den jemand gesucht hat.
    """
    import logging

    from moneten.main import _OhneSuchbegriffe

    eintrag = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.10:0", "GET", "/transactions?q=Zahnarzt&von=2026-01-01", "1.1", 200),
        None,
    )
    assert _OhneSuchbegriffe().filter(eintrag) is True
    text = eintrag.getMessage()
    assert "Zahnarzt" not in text, text
    assert "/transactions" in text, "Der Pfad selbst muss stehenbleiben"
    assert "GET" in text and "200" in text, "Methode und Status gehören ins Protokoll"


def test_ohne_fragezeichen_bleibt_die_zeile_unveraendert() -> None:
    """Ein Filter, der auch dort schneidet, wo nichts ist, macht Protokolle unlesbar."""
    import logging

    from moneten.main import _OhneSuchbegriffe

    eintrag = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.10:0", "GET", "/transactions", "1.1", 200),
        None,
    )
    _OhneSuchbegriffe().filter(eintrag)
    assert eintrag.getMessage().count("?") == 0


# ---------------------------------------------------------------------------
# Greift die Verschlüsselung wirklich?
# ---------------------------------------------------------------------------
def test_offene_datenbank_mit_gesetztem_schluessel_bricht_den_start_ab(
    tmp_path, monkeypatch
) -> None:
    """Der stillste denkbare Fehler: der Schlüssel ist gesetzt, wirkt aber nicht.

    Fehlt ``sqlcipher3`` im Abbild oder stimmt die DB-URL nicht, läuft die App
    weiter — nur eben mit einer offenen Datei, in die jede weitere Buchung im
    Klartext geschrieben wird. Niemand merkt es, denn es funktioniert ja alles.
    """
    import pytest

    from moneten.config import settings
    from moneten.db.session import verschluesselung_pruefen

    offen = tmp_path / "offen.db"
    offen.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    monkeypatch.setattr(settings, "db_key", "irgendein-schluessel")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{offen}")

    with pytest.raises(RuntimeError, match="UNVERSCHLÜSSELT"):
        verschluesselung_pruefen()


def test_ohne_schluessel_wird_nichts_geprueft(tmp_path, monkeypatch) -> None:
    """Lokal läuft die App bewusst im Klartext — das ist kein Fehler."""
    from moneten.config import settings
    from moneten.db.session import verschluesselung_pruefen

    offen = tmp_path / "offen.db"
    offen.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr(settings, "db_key", None)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{offen}")

    assert verschluesselung_pruefen() == {}


# ---------------------------------------------------------------------------
# Cookie-Shadowing aus dem eigenen Netz
# ---------------------------------------------------------------------------
def test_im_betrieb_traegt_das_sitzungscookie_den_host_riegel(client, monkeypatch) -> None:
    """Im Tailnet enden alle Geräte gleich, nur vorne steht ein anderer Name.

    Ein übernommener Nachbar-Dienst dort kann ein Cookie mit
    ``Domain=.<gemeinsame-endung>`` setzen; der Browser schickt es dieser App mit,
    und sie kann nicht unterscheiden, welches ihres ist. ``__Host-`` lässt der
    Browser nur ohne Domain und nur mit ``Secure`` zu — damit ist der Weg zu.
    """
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    antwort = client.post("/login", data={"pin": "424242"}, follow_redirects=False)

    gesetzt = [w for k, w in antwort.headers.raw if k.lower() == b"set-cookie"]
    zeile = next((w.decode() for w in gesetzt if "moneten_session" in w.decode()), "")
    assert zeile, f"Kein Sitzungscookie gesetzt: {gesetzt}"
    assert zeile.startswith("__Host-moneten_session="), zeile
    assert "Secure" in zeile and "Path=/" in zeile, zeile
    assert "Domain=" not in zeile, "Mit Domain verliert __Host- seine Wirkung"


def test_im_entwicklungsmodus_bleibt_der_schlichte_name(client, monkeypatch) -> None:
    """Über http verwirft der Browser ein __Host-Cookie kommentarlos.

    Man suchte den Fehler dann in der Anmeldung — und nicht im Cookie-Namen.
    """
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", True)
    antwort = client.post("/login", data={"pin": "424242"}, follow_redirects=False)
    zeile = next(
        (w.decode() for k, w in antwort.headers.raw
         if k.lower() == b"set-cookie" and b"moneten_session" in w), ""
    )
    assert zeile.startswith("moneten_session="), zeile


def test_eine_laufende_sitzung_ueberlebt_die_umstellung(client, monkeypatch) -> None:
    """Eine Härtung, die den Benutzer aussperrt, wird beim nächsten Mal weggelassen."""
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", True)
    client.post("/login", data={"pin": "424242"}, follow_redirects=False)
    altes = client.cookies.get("moneten_session")
    assert altes, "Kein Cookie unter dem alten Namen"

    monkeypatch.setattr(settings, "dev_mode", False)
    antwort = client.get("/", follow_redirects=False)
    assert antwort.status_code < 400, (
        f"Die laufende Sitzung wurde durch die Umstellung ungültig ({antwort.status_code})"
    )


# ---------------------------------------------------------------------------
# Genau EINE Start-PIN im Protokoll
# ---------------------------------------------------------------------------
def test_die_konfiguration_wuerfelt_keine_start_pin(monkeypatch) -> None:
    """Sie wird in jedem Prozess gebaut — auch im Migrationslauf.

    Vorher zog jeder davon seine eigene Zahl und meldete sie: im Startprotokoll
    standen zwei „Start-PINs" untereinander, und die zuerst genannte gehörte zum
    Migrationslauf und war tot. Wer sie abschrieb, kam nicht hinein — und hatte
    keinen Grund, an der Zahl zu zweifeln.
    """
    from moneten.config import Settings

    monkeypatch.delenv("MONETEN_INITIAL_PIN", raising=False)
    a = Settings(_env_file=None)
    b = Settings(_env_file=None)
    assert a.initial_pin == "", f"Die Konfiguration würfelt beim Bauen: {a.initial_pin!r}"
    assert b.initial_pin == ""


def test_gewuerfelt_wird_beim_anlegen_des_benutzers(monkeypatch, caplog) -> None:
    """Dort, wo die PIN wirklich gilt — und dann auch genau einmal gemeldet."""
    import logging

    from moneten.config import settings
    from moneten.db.seeds import _start_pin

    monkeypatch.setattr(settings, "initial_pin", "")
    with caplog.at_level(logging.WARNING):
        pin = _start_pin()

    assert len(pin) == 6 and pin.isdigit(), pin
    meldungen = [r.getMessage() for r in caplog.records if "Start-PIN" in r.getMessage()]
    assert len(meldungen) == 1, meldungen
    assert pin in meldungen[0], "Die gemeldete Zahl ist nicht die, die gilt"


def test_eine_leere_schluesseldatei_wird_gefuellt(tmp_path, monkeypatch) -> None:
    """Sonst bekommt jeder Start einen anderen flüchtigen Schlüssel — still.

    Die Folge sieht man nur am Handy: nach jedem Neustart abgemeldet. Auf eine
    leere Datei als Ursache kommt niemand.
    """
    from moneten.config import Settings

    (tmp_path / "attachments").mkdir()
    leer = tmp_path / "secret_key"
    leer.write_text("", encoding="utf-8")

    monkeypatch.delenv("MONETEN_SECRET_KEY", raising=False)
    s = Settings(_env_file=None, attachments_dir=tmp_path / "attachments")

    assert leer.read_text(encoding="utf-8").strip(), "Die leere Datei blieb leer"
    assert s.secret_key == leer.read_text(encoding="utf-8").strip()

    # Und der nächste Start nimmt denselben — sonst wäre nichts gewonnen.
    zweite = Settings(_env_file=None, attachments_dir=tmp_path / "attachments")
    assert zweite.secret_key == s.secret_key


# ---------------------------------------------------------------------------
# Was im Browser liegenbleibt
# ---------------------------------------------------------------------------
def test_seiten_mit_zahlen_werden_nicht_zwischengespeichert(logged_in_client) -> None:
    """Ohne Anweisung entscheidet der Browser — und er entscheidet oft für „behalten".

    Sichtbar wurde das am „Zurück" nach dem Abmelden: die Übersicht stand wieder
    da, ohne Sitzung, aus dem Zwischenspeicher.
    """
    for pfad in ("/", "/transactions", "/login"):
        kopf = logged_in_client.get(pfad).headers.get("cache-control", "")
        assert "no-store" in kopf, f"{pfad}: Cache-Control={kopf!r}"


def test_statische_dateien_behalten_ihre_lange_gueltigkeit(client, monkeypatch) -> None:
    """Sonst lädt die App über eine langsame Leitung bei jeder Navigation neu.

    Die Trennung ist der ganze Punkt: Zahlen sind flüchtig, Schriften nicht.
    Im Entwicklungsmodus gilt bewusst auch für sie ``no-store`` — sonst sieht man
    seine eigene CSS-Änderung nicht. Geprüft wird darum der Betriebsfall.
    """
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    kopf = client.get("/static/js/app.js").headers.get("cache-control", "")
    assert "no-store" not in kopf, kopf
    assert "max-age" in kopf, kopf


def test_abmelden_loescht_das_cookie_auch_im_betrieb(client, monkeypatch) -> None:
    """Ein ``__Host-``-Cookie nimmt der Browser nur mit ``Secure`` an — auch das Lösch-Cookie.

    Ohne das Flag verwirft er die Löschung stillschweigend: das Abmelden sieht
    erfolgreich aus, die Sitzung bleibt bestehen. Geprüft werden deshalb die
    Eigenschaften der Lösch-Anweisung, nicht nur ihr Vorhandensein.
    """
    from moneten.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    client.post("/login", data={"pin": "424242"}, follow_redirects=False)
    antwort = client.get("/logout", follow_redirects=False)

    zeilen = [w.decode() for k, w in antwort.headers.raw
              if k.lower() == b"set-cookie" and b"moneten_session" in w]
    assert zeilen, f"Keine Lösch-Anweisung: {antwort.headers.raw}"
    host_zeile = next((z for z in zeilen if z.startswith("__Host-")), "")
    assert host_zeile, f"Das __Host--Cookie wird nicht gelöscht: {zeilen}"
    assert "Secure" in host_zeile, f"Ohne Secure verwirft der Browser die Löschung: {host_zeile}"
    assert "Path=/" in host_zeile and "Domain=" not in host_zeile, host_zeile


def test_der_healthcheck_filter_filtert_wirklich() -> None:
    """Er tat es nie: die Statuszahl steht am Zeilenende, ``" 200 "`` trifft dort nicht.

    Der frühere Test prüfte eine Zeilenform, die uvicorn gar nicht erzeugt —
    grün, und trotzdem lief jede der 2'880 täglichen Healthcheck-Zeilen ins
    Protokoll. Geprüft wird deshalb mit den Argumenten, die uvicorn wirklich
    übergibt.
    """
    import logging

    from moneten.main import _OhneGesundeHealthchecks

    def zeile(pfad: str, status: int) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %d',
            ("192.0.2.10:0", "GET", pfad, "1.1", status), None,
        )

    filt = _OhneGesundeHealthchecks()
    assert filt.filter(zeile("/health", 200)) is False, "Der gesunde Healthcheck steht im Protokoll"
    # Was NICHT weggefiltert werden darf:
    assert filt.filter(zeile("/health", 503)) is True, "Ein kranker Healthcheck fehlt im Protokoll"
    assert filt.filter(zeile("/transactions", 200)) is True
    assert filt.filter(zeile("/healthcheck-irgendwas", 200)) is True
