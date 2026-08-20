"""Tests für die Anzeige-Filter aus :mod:`moneten.templating`.

Diese Filter ändern NUR die Darstellung. Gespeichert, gesucht und kategorisiert
wird immer der Originaltext — genau das sichern die Tests hier ab.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from moneten.templating import chf, chf_kurz, chf_wert, desc_kurz


class TestChfKurz:
    """Dichte Listen: ohne Währung, ohne Rappen, Null als Gedankenstrich."""

    @pytest.mark.parametrize(("wert", "erwartet"), [
        # Decimal formatiert mit ROUND_HALF_EVEN — .50 rundet auf die gerade
        # Ziffer, 1234.50 wird also 1'234 und nicht 1'235. Für eine gerundete
        # Übersichtsanzeige unerheblich, hier aber festgehalten, damit die
        # Wahl bewusst bleibt und nicht als Fehler „korrigiert" wird.
        (Decimal("1234.50"), "1'234"),
        (Decimal("1235.50"), "1'236"),
        (Decimal("0"), "–"),
        (None, "–"),
        # Echtes Minuszeichen (U+2212), nicht der Bindestrich: ``chf_wert``
        # machte es schon so, ``chf``/``chf_kurz`` nicht — in derselben Zeile
        # standen dann „−900" und „-150" nebeneinander.
        (Decimal("-89.20"), "−89"),
        (Decimal("1000000"), "1'000'000"),
    ])
    def test_formatierung(self, wert: Decimal | None, erwartet: str) -> None:
        assert chf_kurz(wert) == erwartet


class TestChfWert:
    """Buchungsliste: ohne Währung, aber mit Rappen."""

    def test_ohne_waehrung_mit_rappen(self) -> None:
        assert chf_wert(Decimal("1234.50")) == "1'234.50"

    def test_null_bleibt_eine_zahl(self) -> None:
        """Anders als chf_kurz: eine Buchung über 0.00 ist eine echte Buchung."""
        assert chf_wert(Decimal("0")) == "0.00"

    def test_typografisches_minus(self) -> None:
        assert chf_wert(Decimal("-42.10")) == "−42.10"

    def test_chf_bleibt_unveraendert(self) -> None:
        """Die Leitzahlen nennen die Währung weiterhin."""
        assert chf(Decimal("1234.50")) == "CHF 1'234.50"


class TestDescKurz:
    """Bank-Präfixe wegkürzen, ohne den Text zu zerstören."""

    def test_e_banking_praefix(self) -> None:
        assert desc_kurz("E-Banking Auftrag an Coop Rechtsschutz") == "Coop Rechtsschutz"

    def test_laengster_praefix_gewinnt(self) -> None:
        """Sonst bliebe von „E-Banking Auftrag an X" ein „Auftrag an X" übrig."""
        assert desc_kurz("E-Banking Auftrag an McDonald's Musterstadt") == "McDonald's Musterstadt"

    def test_bindewort_faellt_mit(self) -> None:
        """Der Fall, der mich am ursprünglichen Vorschlag gestört hat:
        „Zahlung an Penelope" darf nicht zu „an Penelope" werden."""
        assert desc_kurz("Zahlung an Penelope") == "Penelope"
        assert desc_kurz("Kauf bei Beispielshop") == "Beispielshop"

    def test_bindewort_am_anfang_bleibt_ohne_praefix(self) -> None:
        """Ohne vorangehendes Präfix ist „Von …" der Text selbst."""
        assert desc_kurz("Von Oma zum Geburtstag") == "Von Oma zum Geburtstag"

    def test_betrag_im_text_bleibt(self) -> None:
        """Die Uhrzeit-Regex greift nur mit Doppelpunkt — sonst wäre hier der
        Betrag verschwunden."""
        assert desc_kurz("Migros 12.50") == "Migros 12.50"

    def test_uhrzeit_faellt_weg(self) -> None:
        assert desc_kurz("Supermarkt Musterstadt 18:32") == "Supermarkt Musterstadt"

    def test_kartennummer_faellt_weg(self) -> None:
        assert desc_kurz("Grossverteiler Musterort Karte 1234") == "Grossverteiler Musterort"

    def test_leere_beschreibung(self) -> None:
        assert desc_kurz("") == "(ohne Beschreibung)"
        assert desc_kurz(None) == "(ohne Beschreibung)"

    def test_nur_praefix_faellt_auf_original_zurueck(self) -> None:
        """Lieber der Originaltext als eine leere Zeile."""
        assert desc_kurz("E-Banking Auftrag an") == "E-Banking Auftrag an"

    def test_gewoehnlicher_text_unveraendert(self) -> None:
        for t in ["Miete Wohnung", "Netflix Abo", "Lohn Arbeitgeber AG", "TWINT an Penelope"]:
            assert desc_kurz(t) == t

    def test_mehrfache_leerzeichen_normalisiert(self) -> None:
        assert desc_kurz("  Supermarkt   Musterstadt  ") == "Supermarkt Musterstadt"
