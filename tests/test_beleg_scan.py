

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


def test_ein_haengender_auftrag_gibt_seinen_platz_wieder_frei() -> None:
    """Sonst genügt ein Hänger, um einen der vier Plätze für immer zu belegen.

    Die Erkennung läuft in einem Thread, den niemand abbrechen kann. Hängt sie
    — verklemmter Unterprozess, Modell in einer Schleife —, bleibt der Auftrag
    auf „läuft". Vier solche Fälle, und die App nimmt keinen Beleg mehr an,
    ohne dass irgendwo steht, warum. Der Thread darf weiterlaufen; was er nicht
    darf, ist einen Platz blockieren.
    """
    import time

    from moneten.services import jobs

    jobs._SCANS.clear()
    try:
        jobs._SCANS["haenger"] = {
            "zustand": "laeuft", "ocr": None, "bild": None, "fehler": "",
            "begonnen": time.monotonic() - jobs._HOECHSTDAUER_SEKUNDEN - 1,
        }
        jobs._SCANS["frisch"] = {
            "zustand": "laeuft", "ocr": None, "bild": None, "fehler": "",
            "begonnen": time.monotonic(),
        }

        assert jobs.offene_scans() == 1, "Der Hänger zählt weiter als offen"
        assert jobs._SCANS["haenger"]["zustand"] == "fehler"
        assert "Zeit" in jobs._SCANS["haenger"]["fehler"]
        assert jobs._SCANS["frisch"]["zustand"] == "laeuft", "Der frische wurde mitgerissen"
    finally:
        jobs._SCANS.clear()


def test_die_erkennung_hat_eine_frist() -> None:
    """Ohne ``timeout=`` wartet ``communicate()`` unbegrenzt auf Tesseract."""
    from pathlib import Path

    quelle = (Path(__file__).resolve().parents[1] / "src/moneten/services/receipt_ocr.py").read_text(
        encoding="utf-8"
    )
    aufrufe = [z for z in quelle.splitlines() if "image_to_string(" in z]
    assert aufrufe, "Keine Tesseract-Aufrufe gefunden — Test veraltet?"
    ohne_frist = [z.strip() for z in aufrufe if "timeout" not in z]
    # Mehrzeilige Aufrufe: die Frist darf auch in der Folgezeile stehen.
    ohne_frist = [z for z in ohne_frist if not z.endswith("(")]
    assert not ohne_frist, f"Tesseract-Aufruf ohne Frist: {ohne_frist}"
