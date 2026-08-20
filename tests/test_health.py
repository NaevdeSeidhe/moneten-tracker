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
    """
    import logging

    from moneten.main import _OhneGesundeHealthchecks

    filt = _OhneGesundeHealthchecks()

    def _satz(text: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, text, None, None)

    assert not filt.filter(_satz('127.0.0.1:38136 - "GET /health HTTP/1.1" 200 OK'))
    # Alles andere bleibt — auch ein KRANKER Healthcheck.
    assert filt.filter(_satz('127.0.0.1:38136 - "GET /health HTTP/1.1" 500 Internal Server Error'))
    assert filt.filter(_satz('127.0.0.1:38136 - "GET /budget HTTP/1.1" 200 OK'))
    assert filt.filter(_satz('[moneten] Privilegien abgeben — UID 10001, GID 10001'))
