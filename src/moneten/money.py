"""Geld-Hilfsfunktionen.

``parse_amount`` wird von mehreren Routern genutzt (Konten, Buchungen), daher
zentral hier statt dupliziert. Die Anzeige-Formatierung (``chf``) liegt als
Jinja-Filter in ``templating.py``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_amount(raw: str) -> Decimal:
    """Parst Betragseingaben tolerant und gibt einen auf 2 Stellen gerundeten Decimal.

    Akzeptiert ``1234.50``, ``1'234.50`` (CH-Tausendertrenner) und ``1234,50``
    (Komma als Dezimal). Leere Eingabe ergibt 0. Wirft ``InvalidOperation`` bei
    ungültigen Eingaben (vom Aufrufer abzufangen) — inklusive der sonst gültigen
    Decimal-Sonderwerte ``NaN``/``Infinity``, die niemals als Geldbetrag taugen.
    """
    s = (raw or "").strip().replace("'", "").replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        # Beide Trenner vorhanden: der LETZTE ist der Dezimaltrenner. „1.234,50"
        # (deutsch: Punkt=Tausender) wurde sonst zu 1.23 verstümmelt.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")  # „1,234.50" — Komma als Tausendertrenner
    elif "," in s:
        s = s.replace(",", ".")  # Komma als Dezimaltrenner
    if s == "":
        return Decimal("0")
    value = Decimal(s)
    if not value.is_finite():  # "nan", "inf", "-Infinity" → ablehnen
        raise InvalidOperation("Betrag muss eine endliche Zahl sein.")
    return value.quantize(Decimal("0.01"))
