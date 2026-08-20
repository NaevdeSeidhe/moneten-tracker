"""Ein winziges Bild, das riesig behauptet zu sein, darf nicht durchkommen.

Die Grössenprüfung des Uploads misst die DATEI. Eine einfarbige Fläche von
20 000 × 20 000 Punkten komprimiert auf wenige Kilobyte — sie besteht diese
Prüfung mühelos und belegt beim Entpacken 400 Megabyte. Der Container hat ein
Gigabyte, und die Erkennung läuft daneben.

Pillow hat dafür eine eigene Grenze, sie liegt aber bei rund 179 Megapixeln —
also über einem halben Gigabyte. Diese Tests halten fest, dass die App eine
eigene, deutlich engere setzt, und zwar BEVOR ein Bild geöffnet wird.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from moneten.services.receipt_ocr import MAX_BILDPUNKTE, extract_text_from_bytes, pil_image


def _png_mit_ausmassen(breite: int, hoehe: int) -> bytes:
    """Ein gültiges PNG, das ``breite × hoehe`` ANKÜNDIGT — ohne so gross zu sein.

    Genau das macht eine Bildbombe aus: der Kopf verspricht die Fläche, die
    Daten liefern sie nicht. Gebaut wird die Datei von Hand, weil ein echtes
    Bild dieser Grösse schon beim Erzeugen den Arbeitsspeicher sprengte — der
    Test wäre dann selbst die Bombe.
    """
    def block(art: bytes, inhalt: bytes) -> bytes:
        return (struct.pack(">I", len(inhalt)) + art + inhalt
                + struct.pack(">I", zlib.crc32(art + inhalt) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", breite, hoehe, 8, 0, 0, 0, 0)  # 8 bit, Graustufen
    # Eine einzige leere Zeile als Bilddaten. Pillow liest die Ausmasse aus dem
    # Kopf und schlägt zu, lange bevor es hier ankommt.
    idat = zlib.compress(b"\x00" + b"\x00" * min(breite, 1024))
    return b"\x89PNG\r\n\x1a\n" + block(b"IHDR", ihdr) + block(b"IDAT", idat) + block(b"IEND", b"")


def test_die_grenze_ist_enger_als_die_von_pillow() -> None:
    """Pillows Vorgabe schützt einen 1-GB-Container nicht.

    Sie liegt bei rund 179 Megapixeln — entpackt über 500 MB. Die Grenze muss
    deutlich darunter liegen, sonst ist sie nur Zierde.
    """
    assert MAX_BILDPUNKTE <= 60_000_000
    assert pil_image().MAX_IMAGE_PIXELS == MAX_BILDPUNKTE


def test_uebergrosses_bild_wird_abgewiesen() -> None:
    """20 000 × 20 000 = 400 Megapixel — Pillow muss das ablehnen."""
    Image = pil_image()
    from PIL import Image as _PIL

    bombe = _png_mit_ausmassen(20_000, 20_000)
    assert len(bombe) < 20_000, "die Testdatei selbst muss klein sein"
    with pytest.raises(_PIL.DecompressionBombError):
        Image.open(io.BytesIO(bombe))


def test_der_upload_weg_stuerzt_daran_nicht_ab() -> None:
    """Die Erkennung antwortet leer statt zu krachen.

    Ein 500er wäre hier das kleinere Übel, aber immer noch eines: die Sperre
    soll den Beleg abweisen, nicht die Seite.
    """
    ergebnis = extract_text_from_bytes(_png_mit_ausmassen(20_000, 20_000), ".png")
    assert ergebnis.method == "none"
    assert not (ergebnis.text or "").strip()


def test_ein_gewoehnliches_belegfoto_geht_weiterhin_durch() -> None:
    """Die Grenze darf keine echten Fotos treffen.

    Ein Handyfoto liegt bei 12 bis 50 Megapixeln. Eine Sperre, die die trifft,
    wäre schlimmer als keine — man schaltete sie ab.
    """
    Image = pil_image()
    bild = Image.open(io.BytesIO(_png_mit_ausmassen(4000, 3000)))
    assert bild.size == (4000, 3000)
