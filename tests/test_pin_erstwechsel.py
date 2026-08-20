"""Solange die Start-PIN gilt, ist die App zu.

Der Grund ist kein hypothetischer: die Start-PIN steht in ``.env.example``, also
in einer Datei, die jeder liest, der die App aufsetzt — und mancher lässt sie
stehen. Ein Hinweisbanner hätte das nicht verhindert; Banner klickt man weg.

Geprüft wird deshalb nicht, ob irgendwo ein Hinweis erscheint, sondern ob die
Daten wirklich unerreichbar bleiben.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.auth.pin import verify_pin
from moneten.db.models import User
from moneten.db.session import SessionLocal

START_PIN = "424242"


@pytest.fixture
def start_pin_gilt() -> Iterator[None]:
    """Setzt den Benutzer auf „PIN nie gewechselt" und stellt danach wieder her."""
    with SessionLocal() as db:
        user = db.get(User, 1)
        vorher = user.pin_changed_at
        vorher_hash = user.pin_hash
        user.pin_changed_at = None
        db.commit()
    yield
    with SessionLocal() as db:
        user = db.get(User, 1)
        user.pin_changed_at = vorher or datetime.now(UTC)
        user.pin_hash = vorher_hash
        db.commit()


def _angemeldet(client: TestClient) -> None:
    antwort = client.post("/login", data={"pin": START_PIN}, follow_redirects=False)
    assert antwort.status_code in (303, 204), antwort.text


def test_start_pin_sperrt_jede_seite(client: TestClient, start_pin_gilt) -> None:
    """Nicht nur das Dashboard — jede geschützte Route.

    Eine Sperre, die nur die Startseite kennt, ist keine: die Buchungsliste
    liegt einen Link weiter, und ein Kontoauszug ist kein geringerer Einblick
    als eine Übersicht.
    """
    _angemeldet(client)
    for pfad in ("/", "/transactions", "/accounts", "/budget", "/settings"):
        antwort = client.get(pfad, follow_redirects=False)
        assert antwort.status_code == 303, f"{pfad} war trotz Start-PIN erreichbar"
        assert antwort.headers["location"] == "/pin-aendern", pfad


def test_wechselseite_und_abmelden_bleiben_offen(client: TestClient, start_pin_gilt) -> None:
    """Sonst wäre die Sperre eine Sackgasse.

    Ohne diese Ausnahmen käme man weder zur Wechsel-Seite noch wieder hinaus —
    die App wäre für den, den sie schützen soll, unbenutzbar.
    """
    _angemeldet(client)
    assert client.get("/pin-aendern", follow_redirects=False).status_code == 200
    assert client.get("/logout", follow_redirects=False).status_code == 303


def test_htmx_bekommt_den_redirect_header(client: TestClient, start_pin_gilt) -> None:
    """HTMX folgt keinem 303 im Rumpf, sondern dem Header.

    Ohne ihn bliebe der Bildschirm einfach stehen — die Sperre wäre unsichtbar
    und sähe wie ein Fehler aus.
    """
    _angemeldet(client)
    antwort = client.get("/transactions", headers={"HX-Request": "true"},
                         follow_redirects=False)
    assert antwort.status_code == 403
    assert antwort.headers.get("HX-Redirect") == "/pin-aendern"


def test_eigene_pin_hebt_die_sperre_auf(client: TestClient, start_pin_gilt) -> None:
    """Der ganze Weg: sperren, wechseln, drin sein — und die alte PIN gilt nicht mehr."""
    _angemeldet(client)
    antwort = client.post("/pin-aendern",
                          data={"neue_pin": "907314", "bestaetigung": "907314"},
                          follow_redirects=False)
    assert antwort.status_code == 303 and antwort.headers["location"] == "/"
    assert client.get("/transactions", follow_redirects=False).status_code == 200

    client.get("/logout")
    assert client.post("/login", data={"pin": START_PIN},
                       follow_redirects=False).status_code == 400
    assert client.post("/login", data={"pin": "907314"},
                       follow_redirects=False).status_code in (303, 204)


@pytest.mark.parametrize(
    ("neu", "bestaetigung", "grund"),
    [
        ("907314", "907315", "Tippfehler in der Wiederholung"),
        ("12345", "12345", "zu kurz"),
        ("abcdef", "abcdef", "keine Ziffern"),
        (START_PIN, START_PIN, "die bisherige PIN"),
        ("123456", "123456", "aufsteigende Reihe"),
        ("654321", "654321", "absteigende Reihe"),
        ("111111", "111111", "sechs gleiche Ziffern"),
    ],
)
def test_schwache_oder_falsche_eingabe_wird_abgelehnt(
    client: TestClient, start_pin_gilt, neu: str, bestaetigung: str, grund: str
) -> None:
    """Abgelehnt wird mit 400 — und die Sperre steht danach noch.

    Die Reihen sind keine Theorie: 123456 und 111111 sind die ersten beiden
    Versuche jeder Liste. Eine Regel, die sie durchlässt, verschiebt das
    Problem nur von der Konfigurationsdatei in die Gewohnheit.
    """
    _angemeldet(client)
    antwort = client.post("/pin-aendern",
                          data={"neue_pin": neu, "bestaetigung": bestaetigung},
                          follow_redirects=False)
    assert antwort.status_code == 400, grund
    assert client.get("/", follow_redirects=False).status_code == 303, (
        f"nach abgelehnter Eingabe ({grund}) war die App offen"
    )


def test_seeds_legen_den_benutzer_ohne_wechsel_an() -> None:
    """Eine frische Installation startet gesperrt.

    Das ist der eigentliche Fall: wer die App zum ersten Mal aufsetzt, hat die
    PIN aus der Beispieldatei — und soll gar nicht erst dazu kommen, sie zu
    vergessen.
    """
    from moneten.db.seeds import seed_user

    with SessionLocal() as db:
        user = db.get(User, 1)
        gemerkt = user.pin_changed_at
        db.delete(user)
        db.commit()
        neu = seed_user(db)
        assert neu.pin_changed_at is None
        neu.pin_changed_at = gemerkt or datetime.now(UTC)
        db.commit()


def test_erstwechsel_ist_nach_dem_wechsel_zu(client: TestClient, start_pin_gilt) -> None:
    """Der Weg ohne Abfrage der alten PIN darf nur EINMAL offenstehen.

    **Gemessen, nicht vermutet.** Die GET-Route prüfte seit jeher, ob die
    Start-PIN noch gilt; die POST-Route daneben nicht. Damit liess sich die PIN
    jederzeit neu setzen, ohne die alte zu kennen — man brauchte nur eine
    gültige Sitzung. Aus geliehenem Zugriff (fremdes Handy, kopiertes Cookie)
    wurde so dauerhafter, und der Besitzer war ausgesperrt.

    Der reguläre Weg über die Einstellungen verlangt die aktuelle PIN. Diese
    Tür muss deshalb zufallen, sobald sie ihren Zweck erfüllt hat.
    """
    _angemeldet(client)

    # Erster Wechsel: erlaubt, denn es gilt noch die Start-PIN.
    erst = client.post(
        "/pin-aendern", data={"neue_pin": "739104", "bestaetigung": "739104"},
        follow_redirects=False,
    )
    assert erst.status_code in (200, 303), erst.status_code

    # Zweiter Versuch mit derselben Sitzung: muss abgewiesen werden.
    zweit = client.post(
        "/pin-aendern", data={"neue_pin": "482915", "bestaetigung": "482915"},
        follow_redirects=False,
    )
    assert zweit.status_code == 303, (
        f"Die PIN liess sich ein zweites Mal ohne die alte setzen ({zweit.status_code})"
    )

    # Und die zweite PIN gilt NICHT — der Wechsel hat gar nicht stattgefunden.
    with SessionLocal() as db:
        user = db.scalars(select(User)).first()
        assert verify_pin("739104", user.pin_hash), "Die abgewiesene PIN wurde trotzdem gesetzt"
