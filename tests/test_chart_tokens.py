"""Diagrammfarben: eine Quelle, zwei Ausgabeorte.

Die Hex-Werte stehen in ``palette.py``. Daraus erzeugt
``scripts/_gen_chart_tokens.py`` den ``--chart-*``-Block je Skin in ``skins.css``,
und der Server schreibt nur noch ``var(--chart-3)`` ins Markup.

Warum überhaupt so: Der Theme-Wechsel läuft im Browser — er setzt ``data-theme``
am ``<html>``, worauf die CSS-Variablen umschalten. Ein server-seitig
eingesetzter Hex-Wert weiss davon nichts. Die Diagramme blieben deshalb in den
Farben stehen, mit denen die Seite geladen wurde, und wurden erst beim nächsten
Seitenaufbau richtig.

Der Preis dieser Lösung ist eine Kopie: dieselben Farben stehen in Python UND in
CSS. Genau dafür sind diese Tests da — sie rechnen die CSS-Seite aus der
Python-Seite nach. Läuft eines der beiden weg, schlagen sie an.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
SKINS = WURZEL / "src" / "moneten" / "static" / "css" / "skins.css"

sys.path.insert(0, str(WURZEL / "scripts"))

SKIN_NAMEN = ("dark", "light", "nord", "synthwave", "ayu-hell", "melange")


def _block(skin: str) -> str:
    """Der CSS-Abschnitt eines Skins aus der laufenden skins.css."""
    m = re.search(r'\[data-theme="' + re.escape(skin) + r'"\] \{(.*?)\n\}', SKINS.read_text(encoding="utf-8"), re.S)
    assert m, f"Skin {skin} fehlt in skins.css"
    return m.group(1)


def test_css_stimmt_mit_der_palette_ueberein() -> None:
    """Der erzeugte Block muss dem entsprechen, was der Generator heute liefert.

    Wer eine Farbe in ``palette.py`` ändert und den Generator nicht laufen lässt,
    bekommt hier eine rote Meldung statt still falscher Diagramme.
    """
    from _gen_chart_tokens import block_fuer

    abweichend = []
    for skin in SKIN_NAMEN:
        soll = block_fuer(skin)
        if soll not in SKINS.read_text(encoding="utf-8"):
            abweichend.append(skin)
    assert not abweichend, (
        "Diese Skins stimmen nicht mehr mit palette.py überein: "
        f"{abweichend}\nAbhilfe: .venv/Scripts/python.exe scripts/_gen_chart_tokens.py"
    )


def test_jeder_skin_hat_die_volle_palette() -> None:
    """Fehlt eine Farbe, greift ein Diagramm still auf die des Vorgängers zurück."""
    from moneten.palette import CHART_COLORS

    n = len(CHART_COLORS)
    for skin in SKIN_NAMEN:
        abschnitt = _block(skin)
        for i in range(n):
            assert f"--chart-{i}:" in abschnitt, f"{skin}: --chart-{i} fehlt"
            assert f"--chart-{i}-on:" in abschnitt, f"{skin}: --chart-{i}-on fehlt"
        for name in ("--chart-income", "--chart-save", "--chart-reserve"):
            assert f"{name}:" in abschnitt, f"{skin}: {name} fehlt"


def test_server_liefert_variablen_keine_hexwerte() -> None:
    """Der Kern der Sache — sonst wäre alles andere wirkungslos."""
    from moneten.palette import chart_colors, color_at, icon_color_at, income_color

    assert color_at(0).startswith("var(--chart-")
    assert color_at(11).startswith("var(--chart-"), "auch zyklisch über die Palette hinaus"
    assert icon_color_at(2) == "var(--chart-2-on)"
    assert income_color().startswith("var(--chart-")
    assert all(c.startswith("var(--chart-") for c in chart_colors())
    assert "#" not in color_at(3), "Ein Hex-Wert hier friert die Farbe beim Seitenaufbau ein"


def test_farbe_haengt_nicht_mehr_am_theme_argument() -> None:
    """Die Aufrufer geben noch ein Theme mit — es darf nichts mehr ändern.

    Täte es das, hätte man wieder zwei Wahrheiten: eine server-seitige und die
    im Browser geltende.
    """
    from moneten.palette import color_at

    assert color_at(1, "nord") == color_at(1, "synthwave") == color_at(1, None)


def test_keine_gebackenen_diagrammfarben_im_markup() -> None:
    """Regressionstest: kein Template darf eine Diagrammfarbe fest verdrahten.

    Ein einzelnes ``fill="#D4784F"`` würde beim Theme-Wechsel stehenbleiben, und
    genau das fällt erst auf, wenn jemand das Theme wechselt und hinsieht.
    Ampel-, Text- und Flächenfarben sind ausgenommen — die kommen aus dem
    Token-Vertrag und sind ohnehin Variablen.
    """
    templates = WURZEL / "src" / "moneten" / "templates"
    hexwert = re.compile(r'(?:fill|stroke)="#[0-9A-Fa-f]{3,8}"')
    treffer = []
    for pfad in sorted(templates.rglob("*.html")):
        if pfad.name == "icon_sprite.html":
            continue  # Icons sind einfarbig über currentColor, keine Diagrammfarben
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if hexwert.search(zeile):
                treffer.append(f"{pfad.name}:{nr}  {zeile.strip()[:70]}")
    assert not treffer, "Fest verdrahtete Farben in SVG-Attributen:\n  " + "\n  ".join(treffer)
