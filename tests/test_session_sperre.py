"""Wie lange bleibt die App angemeldet?

Vorher galt die Sitzung zwei Wochen, absolut ab dem Login — die App öffnete sich
danach vierzehn Tage lang ohne Nachfrage. Auf einem Handy, das man aus der Hand
gibt oder verliert, lagen damit alle Zahlen offen.

Jetzt: 15 Minuten **ohne Nutzung**, gleitend. Beide Eigenschaften brauchen einen
Test, und zwar getrennt:

* Läuft die Frist wirklich ab? Sonst ist die Änderung wirkungslos.
* Setzt Nutzung sie zurück? Ohne das würde man mitten im Arbeiten abgemeldet —
  und drehte die Frist nach zwei Tagen entnervt wieder hoch.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from moneten.auth import pin as pin_modul
from moneten.config import settings


def test_standardfrist_ist_kurz() -> None:
    """Die Voreinstellung entscheidet — kaum jemand ändert sie nachträglich."""
    assert settings.session_max_age_seconds <= 60 * 60, (
        f"Voreingestellt sind {settings.session_max_age_seconds} s. Die App zeigt "
        "Kontostände; eine lange Standardfrist ist hier die falsche Vorgabe."
    )


def test_karenz_ueberdauert_ein_beleg_foto() -> None:
    """Die Sperre beim Zurückkehren darf den Kamera-Weg nicht zerstören.

    Beim Beleg-Foto übernimmt die Kamera-App, die PWA geht in den Hintergrund.
    Mit zu knapper Karenz wäre man beim Zurückkommen abgemeldet — mitsamt der
    Aufnahme, die dann neu gemacht werden muss.
    """
    assert 20 <= settings.session_return_grace_seconds <= 120, (
        f"{settings.session_return_grace_seconds} s Karenz: zu knapp fürs Fotografieren "
        "oder so lang, dass die Sperre nichts mehr bringt"
    )


def test_abgelaufene_sitzung_fuehrt_zurueck_zur_anmeldung(
    logged_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Kern: nach Ablauf zeigt die App keine Zahlen mehr.

    Statt echt zu warten wird die Frist auf null gesetzt — dieselbe Wirkung, in
    Millisekunden. Ein Test, der 15 Minuten schläft, wird nicht ausgeführt.
    """
    assert logged_in_client.get("/").status_code == 200

    monkeypatch.setattr(settings, "session_max_age_seconds", 0)
    time.sleep(1.1)  # eine Sekunde, damit der Zeitstempel sicher älter ist

    resp = logged_in_client.get("/", follow_redirects=False)
    assert resp.status_code in (303, 307, 302), (
        f"Abgelaufene Sitzung liefert {resp.status_code} statt einer Umleitung"
    )
    assert "/login" in resp.headers.get("location", "")


def test_nutzung_setzt_die_frist_zurueck(logged_in_client: TestClient) -> None:
    """Gleitendes Fenster: Wer die App benutzt, bleibt drin.

    Nachgewiesen am Cookie: Jede Antwort erneuert es, der Zeitstempel darin ist
    danach jünger als vorher. Ohne diese Erneuerung wäre die 15-Minuten-Frist
    absolut — und man flöge mitten im Arbeiten raus.
    """
    name = settings.session_cookie_name

    erst = logged_in_client.get("/").cookies.get(name)
    time.sleep(1.1)
    zweit = logged_in_client.get("/budget").cookies.get(name)

    assert zweit, "Die Antwort erneuert das Sitzungs-Cookie nicht"
    assert zweit != erst, (
        "Cookie unverändert — die Frist läuft absolut ab dem Login weiter, "
        "nicht ab der letzten Nutzung"
    )

    # Und der neue Zeitstempel ist wirklich jünger (nicht bloss ein anderer Wert).
    def stempel(roh: str) -> float:
        return pin_modul._signer.unsign(roh, return_timestamp=True)[1].timestamp()

    assert stempel(zweit) > stempel(erst)


def test_statische_dateien_halten_die_sitzung_nicht_wach(
    logged_in_client: TestClient,
) -> None:
    """Sonst wäre die Leerlauf-Frist keine.

    Icons und CSS lädt der Browser im Hintergrund nach, teils aus dem Cache.
    Würde das die Sitzung verlängern, liefe sie faktisch nie ab.
    """
    name = settings.session_cookie_name
    logged_in_client.get("/")
    vorher = logged_in_client.cookies.get(name)

    resp = logged_in_client.get("/static/css/theme.css")
    assert resp.status_code == 200
    assert name not in resp.cookies, (
        "Eine statische Datei erneuert das Sitzungs-Cookie"
    )
    assert logged_in_client.cookies.get(name) == vorher


def test_abmelden_bleibt_abgemeldet(logged_in_client: TestClient) -> None:
    """Regressionsgefahr der Middleware: sie könnte das eben gelöschte Cookie
    unmittelbar wieder setzen und das Abmelden wirkungslos machen."""
    logged_in_client.get("/logout", follow_redirects=False)
    resp = logged_in_client.get("/", follow_redirects=False)
    assert resp.status_code in (303, 307, 302)
    assert "/login" in resp.headers.get("location", "")


def test_karenz_steht_im_markup(logged_in_client: TestClient) -> None:
    """Das Skript liest die Karenz aus dem Markup — die Zahl darf nur an einer
    Stelle stehen (config.py), sonst laufen sie auseinander."""
    seite = logged_in_client.get("/").text
    assert f'data-lock-grace="{settings.session_return_grace_seconds}"' in seite


def test_cookie_bleibt_abgesichert() -> None:
    """httponly/secure/samesite dürfen bei den Änderungen nicht verlorengehen."""
    from fastapi import Response

    monkey = Response()
    frueher = settings.dev_mode
    try:
        settings.dev_mode = False
        pin_modul.issue_session(monkey, 1)
    finally:
        settings.dev_mode = frueher

    gesetzt = monkey.headers.get("set-cookie", "")
    assert "HttpOnly" in gesetzt
    assert "Secure" in gesetzt
    assert "SameSite=lax" in gesetzt
    assert f"Max-Age={settings.session_max_age_seconds}" in gesetzt


def test_frist_ist_per_umgebungsvariable_aenderbar() -> None:
    """Der Nutzer soll sie ohne neues Deploy anpassen können.

    Der Präfix ist Teil des Vertrags mit der Dokumentation: steht dort
    ``MONETEN_SESSION_MAX_AGE_SECONDS``, muss genau das auch greifen.
    """
    from moneten.config import Settings

    assert Settings.model_config["env_prefix"] == "MONETEN_"
    assert "session_max_age_seconds" in Settings.model_fields

    from pathlib import Path

    vorlage = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "MONETEN_SESSION_MAX_AGE_SECONDS" in vorlage, (
        "Eine Einstellung, die niemand kennt, kann niemand anpassen"
    )
