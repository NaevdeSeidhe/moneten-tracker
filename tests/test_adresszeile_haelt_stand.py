"""Was aus der Adresszeile kommt, darf keinen Serverfehler auslösen.

Drei Umwandlungen sahen abgesichert aus und waren es nicht — Zeichen, die als
Ziffer gelten, aber keine sind; Jahreszahlen ausserhalb des Datumsbereichs;
Unendlich. Keine davon lässt jemanden hinein, aber jede erzeugt einen 500er,
und ein 500er sagt dem Gegenüber, dass hier etwas ungeprüft durchgeht.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("wert", ["²³", "12a", "-5", "٣", "1_0", " "])
def test_unsinnige_filter_id_ergibt_keinen_serverfehler(
    logged_in_client: TestClient, wert: str
) -> None:
    antwort = logged_in_client.get("/transactions", params={"account_id": wert})
    assert antwort.status_code < 500, f"{wert!r} → {antwort.status_code}"


@pytest.mark.parametrize("jahr", ["0", "99999", "-1", "10000"])
def test_unsinniges_steuerjahr_ergibt_keinen_serverfehler(
    logged_in_client: TestClient, jahr: str
) -> None:
    antwort = logged_in_client.get("/steuern", params={"jahr": jahr})
    assert antwort.status_code < 500, f"jahr={jahr} → {antwort.status_code}"


@pytest.mark.parametrize("wert", ["inf", "-inf", "1e400", "nan", "abc"])
def test_unendlich_im_stresstest_ergibt_keinen_serverfehler(wert: str) -> None:
    """Hier reicht die Funktion selbst — sie ist die Stelle, die scheiterte."""
    from moneten.routers.forecast import _to_int

    assert isinstance(_to_int(wert, default=0), int)
