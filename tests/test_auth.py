"""Tests rund um PIN-Login, Theme und PIN-Wechsel."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_login_page_renders(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "PIN" in response.text


def test_login_wrong_pin(client: TestClient) -> None:
    response = client.post("/login", data={"pin": "000000"})
    assert response.status_code == 400
    assert "Falsche PIN" in response.text


def test_login_invalid_format(client: TestClient) -> None:
    response = client.post("/login", data={"pin": "abc"})
    assert response.status_code == 400


def test_login_success_and_dashboard(client: TestClient) -> None:
    # Erfolgreicher Login → Redirect oder 204 (bei HTMX).
    response = client.post("/login", data={"pin": "424242"}, follow_redirects=False)
    assert response.status_code == 303
    # Cookie ist gesetzt.
    cookie = response.cookies.get("moneten_session")
    assert cookie

    # Mit Cookie kommt das Dashboard durch.
    response = client.get("/")
    assert response.status_code == 200
    assert "Moneten-Tracker" in response.text


def test_logout_clears_session(logged_in_client: TestClient) -> None:
    logged_in_client.get("/logout", follow_redirects=False)
    # Danach wieder Redirect auf /login.
    response = logged_in_client.get("/", follow_redirects=False)
    assert response.status_code in (303, 307)


def test_pin_change_flow(logged_in_client: TestClient) -> None:
    # Erst falsche aktuelle PIN.
    response = logged_in_client.post(
        "/settings/pin",
        data={"current_pin": "000000", "new_pin": "111222", "confirm_pin": "111222"},
    )
    assert response.status_code == 400

    # Jetzt korrekt — PIN wechseln und gleich zurück wechseln, damit weitere Tests laufen.
    ok = logged_in_client.post(
        "/settings/pin",
        data={"current_pin": "424242", "new_pin": "111222", "confirm_pin": "111222"},
    )
    assert ok.status_code == 200
    assert "erfolgreich" in ok.text

    back = logged_in_client.post(
        "/settings/pin",
        data={"current_pin": "111222", "new_pin": "424242", "confirm_pin": "424242"},
    )
    assert back.status_code == 200


def test_theme_persist(logged_in_client: TestClient) -> None:
    response = logged_in_client.post("/settings/theme", data={"theme": "light"})
    assert response.status_code == 204
    # Settings-Seite zeigt das neue Theme.
    page = logged_in_client.get("/settings")
    assert "light" in page.text

    # Zurücksetzen.
    logged_in_client.post("/settings/theme", data={"theme": "dark"})
