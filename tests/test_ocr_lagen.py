"""Der frühe Abbruch beim Drehen — er darf sparen, aber nichts verlieren.

Vier volle OCR-Lesungen je Foto waren der Grund, warum man am Handy Minuten
vor einem Wartebildschirm sass. Gemessen an einem 12-MP-Testbon mit zwanzig
Zeilen: 9.56 s aufrecht, davon drei Lesungen umsonst.

Geprüft wird hier mit einer **eingesetzten Engine**, nicht mit echtem OCR: die
Frage ist nicht, wie gut RapidOCR liest, sondern wann die Schleife aufhört.
"""

from __future__ import annotations

from moneten.services import receipt_ocr


class _Bild:
    """Ein Bild-Doppel, das nur zählt, wie oft es gedreht wurde."""

    def __init__(self, texte: dict[int, str]) -> None:
        self.texte, self.grad = texte, 0

    def rotate(self, deg: int, expand: bool = False) -> _Bild:
        gedreht = _Bild(self.texte)
        gedreht.grad = (self.grad + deg) % 360
        return gedreht


def _engine(protokoll: list[int]):
    def lesen(bild: _Bild) -> str:
        protokoll.append(bild.grad)
        return bild.texte.get(bild.grad, "")
    return lesen


# Eine Lesung, die richtig herum ist: Namen links, Beträge rechts.
GUT = "Brot 3.50\nMilch 2.20\nKaffee 4.80\nTotal 10.50"
# Dieselben Zeichen, aber ohne Leserichtung — so sieht eine falsche Lage aus.
SCHLECHT = "05.3 torB\n02.2 hcliM"


def test_richtige_lage_beendet_die_suche(monkeypatch):
    """Stimmt die erste Lage, werden die drei anderen nicht mehr gelesen."""
    protokoll: list[int] = []
    monkeypatch.setattr(receipt_ocr, "_ocr_engine", _engine(protokoll))
    text = receipt_ocr._ocr_pil_image(_Bild({0: GUT, 90: SCHLECHT, 180: SCHLECHT, 270: SCHLECHT}))

    assert protokoll == [0], f"es wurde mehr als einmal gelesen: {protokoll}"
    assert text == GUT


def test_querliegender_beleg_kostet_zwei_lesungen(monkeypatch):
    """Aufrecht bringt nichts, die erste Querlage schon — danach ist Schluss."""
    protokoll: list[int] = []
    monkeypatch.setattr(receipt_ocr, "_ocr_engine", _engine(protokoll))
    text = receipt_ocr._ocr_pil_image(_Bild({0: SCHLECHT, 270: GUT, 90: SCHLECHT, 180: SCHLECHT}))

    assert protokoll == [0, 270]
    assert text == GUT


def test_ohne_klares_ergebnis_werden_alle_lagen_gelesen(monkeypatch):
    """Die Abkürzung darf nichts wegnehmen.

    Findet keine Lage eine brauchbare Leserichtung, bleibt es beim vollen
    Durchgang — lieber langsam als ein halb gelesener Beleg.
    """
    protokoll: list[int] = []
    monkeypatch.setattr(receipt_ocr, "_ocr_engine", _engine(protokoll))
    receipt_ocr._ocr_pil_image(_Bild(dict.fromkeys((0, 90, 180, 270), SCHLECHT)))

    assert sorted(protokoll) == [0, 90, 180, 270]


def test_wenige_betraege_reichen_nicht(monkeypatch):
    """Eine Lage mit einer einzigen Zahl ist kein Grund aufzuhören.

    Sonst gewänne eine Lage, in der die Engine zufällig ein paar Ziffern
    erwischt hat, gegen die, die den ganzen Bon liest.
    """
    protokoll: list[int] = []
    monkeypatch.setattr(receipt_ocr, "_ocr_engine", _engine(protokoll))
    receipt_ocr._ocr_pil_image(_Bild({0: "Brot 3.50", 270: GUT, 90: SCHLECHT, 180: SCHLECHT}))

    assert protokoll[:2] == [0, 270], f"nach der mageren Lage wurde nicht weitergelesen: {protokoll}"


def test_kopfueber_haelt_die_suche_nicht_an(monkeypatch):
    """Viele Beträge allein sind kein Grund aufzuhören — die Richtung muss stimmen.

    Der Fall ist gemessen, nicht ausgedacht: an einem aufrecht fotografierten
    Testbon lieferte auch die um 180° gedrehte Lage 19 von 19 Beträgen und
    dieselbe Lesbarkeit. Nur die Leserichtung trennte sie (19 gegen 0). Ohne
    diese Bedingung hörte die Suche bei der ersten ergiebigen Lage auf — und das
    kann die kopfüberstehende sein.
    """
    # Beträge rechts, Namen links = richtig herum. Kopfüber ist es umgekehrt,
    # trägt aber genauso viele Beträge.
    kopfueber = "3.50 Brot\n2.20 Milch\n4.80 Kaffee\n10.50 Total"
    protokoll: list[int] = []
    monkeypatch.setattr(receipt_ocr, "_ocr_engine", _engine(protokoll))
    text = receipt_ocr._ocr_pil_image(
        _Bild({0: kopfueber, 270: SCHLECHT, 90: SCHLECHT, 180: GUT})
    )

    assert protokoll == [0, 270, 90, 180], f"zu frueh aufgehoert: {protokoll}"
    assert text == GUT
