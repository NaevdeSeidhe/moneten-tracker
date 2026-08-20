"""Erzeugt aus dem Roh-Logo ein PNG mit transparentem Hintergrund.

Strategie: Flood-Fill von den Bildrändern aus. Nur der *zusammenhängende*
weisse Hintergrund (von aussen erreichbar) wird transparent gemacht. Innen
liegende weisse Flächen (Augen, Zähne, Geldscheine) bleiben erhalten, weil
sie durch schwarze Outlines vom Hintergrund getrennt sind — ein globales
„alle weissen Pixel löschen" würde sie fälschlich mit entfernen.

Aufruf:
    python scripts/_make_logo.py <quelle> <ziel.png> [zielbreite]
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw

# Toleranz beim Flood-Fill: wie weit darf ein Pixel vom Startwert abweichen,
# um noch als „Hintergrund" zu gelten. Konservativ, damit der Fill nicht in
# die hellgrauen Geldscheine ausläuft.
THRESH = 36
MARK = (255, 0, 255)  # temporäre Markierungsfarbe (Magenta), kommt im Logo nicht vor


def make_transparent(src: str, dst: str, target_w: int = 320) -> None:
    img = Image.open(src).convert("RGB")
    w, h = img.size

    # Saat-Punkte dicht entlang aller vier Ränder, damit auch Buchten des
    # Hintergrunds erreicht werden (nicht nur die vier Ecken).
    step = max(1, min(w, h) // 60)
    seeds: list[tuple[int, int]] = []
    for x in range(0, w, step):
        seeds += [(x, 0), (x, h - 1)]
    for y in range(0, h, step):
        seeds += [(0, y), (w - 1, y)]

    for sx, sy in seeds:
        r, g, b = img.getpixel((sx, sy))
        # Nur von noch hellen, nicht bereits markierten Punkten starten.
        if (r, g, b) != MARK and min(r, g, b) > 200:
            ImageDraw.floodfill(img, (sx, sy), MARK, thresh=THRESH)

    # Markierte Pixel -> voll transparent, Rest bleibt opak.
    rgba = img.convert("RGBA")
    rgba.putdata([
        (0, 0, 0, 0) if (px[0], px[1], px[2]) == MARK else px
        for px in rgba.getdata()
    ])

    # Auf Web-Breite herunterskalieren (scharf via LANCZOS), Verhältnis halten.
    if target_w and target_w < w:
        target_h = round(h * target_w / w)
        rgba = rgba.resize((target_w, target_h), Image.LANCZOS)

    rgba.save(dst, "PNG")
    print(f"gespeichert: {dst} ({rgba.width}x{rgba.height})")


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2]
    tw = int(sys.argv[3]) if len(sys.argv) > 3 else 320
    make_transparent(src, dst, tw)
