"""ZURZEIT UNBENUTZT — die Theme-Auswahl in den Einstellungen ist entfallen.

Die erzeugten Bilder werden von keiner Seite mehr eingebunden; der Wechsel läuft
über die Kopfzeile und das „Mehr"-Blatt, beide ohne Vorschau. Das Skript bleibt
liegen, weil es die einzige Möglichkeit ist, die Bilder wieder zu erzeugen, falls
die Auswahl je zurückkommt — es gibt kein Git-Verzeichnis, aus dem man sie
zurückholen könnte.

Erzeugt pro Theme eine PNG-Vorschau der Farbpalette.

Quelle ist ``static/css/skins.css`` — die Vorschauen werden also NIE von Hand gepflegt, sondern aus
den echten Token generiert. Legst du ein neues Theme an, einmal laufen lassen:

    .venv\\Scripts\\python.exe scripts/_make_skin_previews.py

Ergebnis: ``static/img/skins/<name>.png`` (je 480x270, 16:9). Gezeigt wird eine
Mini-Nachbildung der App (Seite, Karte, Leitzahl, Akzent-Button) plus ein
Farbstreifen mit den Kern-Token — so sieht man Wirkung UND Palette auf einen Blick.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CSS_DIR = ROOT / "src" / "moneten" / "static" / "css"
OUT_DIR = ROOT / "src" / "moneten" / "static" / "img" / "skins"

W, H = 480, 270
# Diese Token landen als Farbstreifen unten im Bild (Reihenfolge = Anzeige).
SWATCHES = [
    ("accent-primary", "Akzent"),
    ("accent-secondary", "Blau"),
    ("accent-tertiary", "Einnahmen"),
    ("dusty-rose", "Ausgaben"),
    ("warn", "Warnung"),
    ("danger", "Fehler"),
]


def _font(size: int, bold: bool = False):
    """Systemschrift suchen; ohne Treffer nimmt Pillow seinen Default."""
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            p = base / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _strip_comments(css: str) -> str:
    """CSS-Kommentare entfernen — sonst wird das ``[data-theme="name"]``-Beispiel
    aus der Anleitung im Dateikopf als echter Skin gelesen (und verschluckt dabei
    den darauffolgenden Block bis zur nächsten schliessenden Klammer)."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _parse_skins() -> dict[str, dict[str, str]]:
    """Liest alle ``[data-theme="x"]``-Blöcke (:root zählt als „dark")."""
    css = _strip_comments((CSS_DIR / "skins.css").read_text(encoding="utf-8"))
    skins: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"\[data-theme=\"([\w-]+)\"\]\s*\{(.*?)\n\}", css, re.S):
        vals = dict(re.findall(r"--([\w-]+):\s*([^;]+);", m.group(2)))
        skins[m.group(1)] = {k: v.strip() for k, v in vals.items()}
    return skins


def _rgb(value: str, fallback: str = "#888888") -> tuple[int, int, int]:
    """#rgb/#rrggbb → Tupel. rgba() wird (ohne Alpha) auf den reinen Ton reduziert."""
    value = (value or "").split("/*")[0].strip()
    m = re.match(r"#([0-9a-fA-F]{3})$", value)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))  # type: ignore[return-value]
    m = re.match(r"#([0-9a-fA-F]{6})", value)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return _rgb(fallback) if value != fallback else (136, 136, 136)


def _draw(name: str, tok: dict[str, str]) -> Image.Image:
    g = lambda k, d="#888888": _rgb(tok.get(k, d), d)  # noqa: E731
    page, surface = g("bg-page", "#1A1917"), g("bg-surface", "#252320")
    prim, sec, ter = g("text-primary", "#FFF"), g("text-secondary", "#AAA"), g("text-tertiary", "#888")
    border = g("border-emphasis", "#3A3835")
    on_accent = g("on-accent", "#1A1917")
    radius = 12

    im = Image.new("RGB", (W, H), page)
    d = ImageDraw.Draw(im)

    # Kopfzeile: Theme-Name
    d.text((20, 16), name.upper(), font=_font(15, bold=True), fill=prim)
    d.text((20, 38), "Farbwelt-Vorschau", font=_font(11), fill=ter)

    # Karte mit Leitzahl (so wirkt das Theme in der App wirklich)
    cx, cy, cw, ch = 20, 64, W - 40, 104
    d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius, fill=surface, outline=border, width=2)
    d.text((cx + 16, cy + 14), "LIQUIDE MITTEL", font=_font(10, bold=True), fill=ter)
    d.text((cx + 16, cy + 32), "CHF 12'345.60", font=_font(28, bold=True), fill=prim)
    d.text((cx + 16, cy + 72), "+1'234.50 diesen Monat", font=_font(11), fill=g("accent-tertiary", "#94A475"))

    # Akzent-Button (zeigt, ob Text auf gefüllter Fläche lesbar ist)
    bx = cx + cw - 116
    d.rounded_rectangle([bx, cy + 62, bx + 100, cy + 90], radius - 4, fill=g("accent-solid", tok.get("accent-primary", "#D97757")))
    d.text((bx + 20, cy + 69), "Buchen", font=_font(12, bold=True), fill=on_accent)

    # Farbstreifen der Kern-Token
    d.text((20, 182), "PALETTE", font=_font(10, bold=True), fill=ter)
    sw, gap, sx, sy = 62, 10, 20, 200
    for key, label in SWATCHES:
        col = g(key)
        d.rounded_rectangle([sx, sy, sx + sw, sy + 34], 6, fill=col, outline=border)
        d.text((sx, sy + 40), label, font=_font(9), fill=sec)
        sx += sw + gap

    return im


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    skins = _parse_skins()
    if not skins:
        raise SystemExit("Keine Skins in skins.css gefunden — Format geändert?")
    for name, tok in sorted(skins.items()):
        img = _draw(name, tok)
        out = OUT_DIR / f"{name}.png"
        img.save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.1f} KB, {len(tok)} Token)")
    print(f"{len(skins)} Vorschauen erzeugt.")


if __name__ == "__main__":
    main()
