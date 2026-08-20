"""Nach einem Validierungsfehler dürfen die getippten Werte nicht verloren gehen.

Vorher rendete jedes dieser vier Formulare nach einem Fehler LEER — man musste
alles neu eintippen, obwohl nur ein Feld beanstandet wurde. Die Tests prüfen
darum nicht die Fehlermeldung (die gab es schon), sondern dass die Rohwerte
zurückkommen.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_buchung_behaelt_werte(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/transactions", data={
        "kind": "ausgabe",
        "amount": "keine-zahl",          # <- der Fehler
        "date": "2026-07-14",
        "account_id": "1",
        "description": "ZZZ-Testbeschreibung",
        "notes": "ZZZ-Notiz",
    })
    assert resp.status_code == 400
    assert "ZZZ-Testbeschreibung" in resp.text, "Beschreibung ging verloren"
    assert "ZZZ-Notiz" in resp.text, "Notiz ging verloren"
    assert "2026-07-14" in resp.text, "Datum ging verloren"
    assert "keine-zahl" in resp.text, "der beanstandete Wert selbst ging verloren"


def test_schnell_erfassen_behaelt_werte(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/quick", data={
        "amount": "0",                    # <- der Fehler (muss > 0 sein)
        "account_id": "1",
        "kind": "einnahme",
        "description": "ZZZ-Schnellnotiz",
    })
    assert resp.status_code == 400
    assert "ZZZ-Schnellnotiz" in resp.text
    # „Einnahme" muss weiterhin gewählt sein, nicht auf „Ausgabe" zurückfallen.
    assert 'value="einnahme" checked' in resp.text.replace("  ", " ")


def test_abo_behaelt_werte(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/subscriptions", data={
        "name": "ZZZ-Testabo",
        "amount": "0",                    # <- der Fehler
        "interval": "monatlich",
        "kind": "abo",
        "notes": "ZZZ-Abonotiz",
    })
    assert resp.status_code == 400
    assert "ZZZ-Testabo" in resp.text
    assert "ZZZ-Abonotiz" in resp.text


def test_kategorie_behaelt_werte(logged_in_client: TestClient) -> None:
    """Leerer Name ist der Fehler — die übrigen Angaben müssen bleiben."""
    resp = logged_in_client.post("/categories", data={
        "name": "",
        "parent_id": "",
        "icon": "coffee",
        "color": "",
        "art": "S",
    })
    assert "value=\"S\"" in resp.text
    # Die gewählte Art darf nicht auf den Default zurückspringen.
    import re
    treffer = re.search(r'<option value="S"\s*[^>]*selected', resp.text)
    assert treffer, "gewählte Art ging verloren"
