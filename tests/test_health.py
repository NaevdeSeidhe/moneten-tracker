"""Smoke-Test: ``/health`` antwortet ohne Login mit 200."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_dashboard_requires_login(client: TestClient) -> None:
    """Ohne Cookie redirected die App auf /login."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (303, 307)
    assert response.headers["location"].endswith("/login")


def test_gesunde_healthchecks_stehen_nicht_im_protokoll() -> None:
    """2'880 gleiche Zeilen am Tag machen jedes Protokoll unlesbar.

    Gemessen beim Suchen eines Deploy-Fehlers: ``docker logs --tail 40`` zeigte
    ausschliesslich Healthcheck-Zeilen. Danach wären die drei Meldungen des
    Entrypoints darin nicht mehr zu finden gewesen.

    **Der Test prüfte lange die falsche Zeile.** Er baute den Satz selbst
    zusammen und hängte hinter den Status noch ein „OK" — erst dadurch traf das
    damalige Muster ``" 200 "``. Uvicorn schreibt aber
    ``… "GET /health HTTP/1.1" 200``, mit der Zahl am Zeilenende; der Filter
    liess also in Wahrheit alles durch, und der Test war grün. Geprüft wird
    deshalb mit den Bestandteilen, die uvicorn wirklich übergibt.
    """
    import logging

    from moneten.main import _OhneGesundeHealthchecks

    filt = _OhneGesundeHealthchecks()

    def _zugriff(pfad: str, status: int) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:38136", "GET", pfad, "1.1", status), None,
        )

    assert not filt.filter(_zugriff("/health", 200))
    # Alles andere bleibt — auch ein KRANKER Healthcheck.
    assert filt.filter(_zugriff("/health", 500))
    assert filt.filter(_zugriff("/budget", 200))

    # Und was gar keine Zugriffszeile ist, wird nie angefasst: lieber eine Zeile
    # zu viel als eine verlorene Meldung.
    eigen = logging.LogRecord("moneten", logging.INFO, "", 0,
                              "[moneten] Privilegien abgeben — UID 10001", None, None)
    assert filt.filter(eigen)
