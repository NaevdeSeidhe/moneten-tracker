

# ---------------------------------------------------------------------------
# Wie viele Erkennungen offen sein dürfen
# ---------------------------------------------------------------------------
def test_zu_viele_gleichzeitige_scans_werden_abgewiesen(logged_in_client) -> None:
    """Die Schranke begrenzte, wie viele RECHNEN — nicht, wie viele warten.

    Jeder wartende Auftrag hält seine Bilddaten fest: bis zu 15 MB, dazu ein
    eigener Thread. Zehnmal auf „Absenden" getippt sind 150 MB in einem
    Container mit 1 GB — und das Bild wird erst beim Aufräumen frei.

    Geprüft wird die Antwort, nicht die Absicht: der überzählige Upload muss
    mit 429 abprallen, bevor irgendetwas eingelesen wird.
    """
    from moneten.services import jobs

    jobs._SCANS.clear()
    try:
        for nr in range(jobs.MAX_OFFENE_SCANS):
            jobs._SCANS[f"platzhalter{nr}"] = {
                "zustand": "laeuft", "ocr": None, "bild": None, "fehler": ""
            }

        antwort = logged_in_client.post(
            "/import/receipts/photo/start",
            files={"photo": ("beleg.jpg", b"\xff\xd8\xff\xe0" + b"x" * 5000, "image/jpeg")},
        )
        assert antwort.status_code == 429, (
            f"Der {jobs.MAX_OFFENE_SCANS + 1}. gleichzeitige Upload wurde angenommen "
            f"({antwort.status_code}) — jeder wartende hält bis zu 15 MB fest."
        )
    finally:
        jobs._SCANS.clear()


def test_der_dienst_selbst_haelt_die_grenze_auch_ein() -> None:
    """Zweite Stelle, dieselbe Regel — für jeden künftigen Aufrufer."""
    import pytest

    from moneten.services import jobs

    jobs._SCANS.clear()
    try:
        for nr in range(jobs.MAX_OFFENE_SCANS):
            jobs._SCANS[f"platzhalter{nr}"] = {
                "zustand": "laeuft", "ocr": None, "bild": None, "fehler": ""
            }
        with pytest.raises(jobs.ZuVieleScans):
            jobs.start_scan_job(b"x", ".jpg", bild_speichern=None)
    finally:
        jobs._SCANS.clear()
