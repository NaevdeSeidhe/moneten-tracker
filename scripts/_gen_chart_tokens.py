"""Diagrammfarben folgen dem Theme-Wechsel sofort.

BISHER: Die Farben wurden server-seitig als Hex-Werte ins HTML gebacken
(`fill="#D4784F"`). Der Theme-Wechsel laeuft dagegen im Browser — er setzt nur
`data-theme` am <html>, worauf die CSS-Variablen umschalten. Die Diagramme
blieben deshalb in den Farben des Themes stehen, mit dem die Seite geladen
wurde. Erst ein Seitenwechsel brachte sie nach; und weil das Speichern der
Vorliebe ein unbeaufsichtigtes fetch() ist, gewann bei schnellem Klicken mal die
eine, mal die andere Wahl -- daher „nach zig Wechseln stimmt es irgendwann".

JETZT: Die Palette steht als CSS-Variablen je Skin. Der Server schreibt nur noch
`var(--chart-3)` ins Markup; welchen Wert das hat, entscheidet der Browser im
Moment des Zeichnens. Damit gilt fuer die Diagramme dasselbe wie fuer alles
andere im Token-Vertrag -- ein Wechsel wirkt sofort und vollstaendig.

Die Hex-Listen in palette.py bleiben die Quelle: aus ihnen wird der CSS-Block
erzeugt. Ein Test rechnet das nach, damit beide nie auseinanderlaufen.
"""
import pathlib
import re
import subprocess
import sys

BASIS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASIS / "src"))

from moneten.palette import (  # noqa: E402
    _SEMANTIK,
    _SEMANTIK_STANDARD,
    PALETTEN,
    icon_color_for,
)

MARKE_START = "  /* ----- Diagramm-Palette (erzeugt aus palette.py) ----- */"
MARKE_ENDE = "  /* ----- Ende Diagramm-Palette ----- */"


def block_fuer(skin: str) -> str:
    """Der CSS-Block eines Skins — genau so erzeugt ihn auch der Test."""
    pal = PALETTEN.get(skin, PALETTEN["dark"])
    einnahmen, sparen, reserve = _SEMANTIK.get(skin, _SEMANTIK_STANDARD)
    zeilen = [MARKE_START]
    for i, hexwert in enumerate(pal):
        zeilen.append(f"  --chart-{i}: {hexwert};")
    for i, hexwert in enumerate(pal):
        zeilen.append(f"  --chart-{i}-on: {icon_color_for(hexwert)};")
    zeilen.append(f"  --chart-income: {einnahmen};")
    zeilen.append(f"  --chart-save: {sparen};")
    zeilen.append(f"  --chart-reserve: {reserve};")
    zeilen.append(MARKE_ENDE)
    return "\n".join(zeilen)


def main() -> None:
    p = BASIS / "src" / "moneten" / "static" / "css" / "skins.css"
    s = p.read_text(encoding="utf-8")

    for skin in ("dark", "light", "nord", "synthwave", "ayu-hell", "melange"):
        muster = re.compile(r'(\[data-theme="' + re.escape(skin) + r'"\] \{\n)')
        m = muster.search(s)
        assert m, f"Skin-Block {skin} nicht gefunden"
        # Vorhandenen Block ersetzen oder neu einfuegen
        block = block_fuer(skin) + "\n"
        anfang = m.end()
        ende_regel = s.index("\n}", anfang)
        abschnitt = s[anfang:ende_regel]
        if MARKE_START in abschnitt:
            alt = abschnitt[abschnitt.index(MARKE_START):abschnitt.index(MARKE_ENDE) + len(MARKE_ENDE) + 1]
            s = s[:anfang] + abschnitt.replace(alt, block, 1) + s[ende_regel:]
        else:
            s = s[:anfang] + block + s[anfang:]
        print(f"  {skin}: {len(PALETTEN.get(skin, PALETTEN['dark']))} Diagrammfarben + 3 semantische")

    p.write_text(s, encoding="utf-8")
    print("skins.css: Diagramm-Palette je Skin geschrieben")


if __name__ == "__main__":
    main()
    r = subprocess.run(["grep", "-c", "--chart-0:", "src/moneten/static/css/skins.css"],
                       cwd=BASIS, capture_output=True, text=True)
    print("Skins mit eigener Palette:", r.stdout.strip())
