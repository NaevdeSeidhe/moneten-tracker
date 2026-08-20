"""Zentrale Diagramm-Palette (CI) + lesbare Icon-Farbe — EINE Quelle.

Damit die ganze App farblich „wie aus einem Guss" wirkt, ziehen ALLE Diagramme
ihre Farben aus dieser Datei: Donut (Übersicht), Treemap (Grösste Ausgaben),
Sankey (Geldfluss), Budget-Allokation und Abos-/Fixkosten-Balken.

**Die Palette hängt am THEME-NAMEN**, nicht an einem Ja/Nein-Schalter. Früher gab
es genau zwei Paletten (warm / „reactor"); ein neues Theme wie *Nord* bekam
dadurch zwangsläufig die warmen Orangetöne — kühles Blaugrau mit knallorangen
Diagrammen darin. Ein Theme muss aber **vollständig** greifen, sonst wirkt der
Wechsel halb erledigt.

Ein neues Theme braucht hier nur einen Eintrag in :data:`PALETTEN`; fehlt er,
gilt die warme Standardpalette.

Diese Farben sind **Flächen** (Segmente, Balken), kein Text. Sie brauchen darum
keine 4.5:1, sollten sich aber deutlich voneinander unterscheiden — die
Zuordnung Segment→Kategorie hängt daran.
"""

from __future__ import annotations

# Kanonische Volltöne in fester Reihenfolge (Index 0 = erstes Segment überall).
# Warm und erdig, passend zu Dunkel und Hell.
CHART_COLORS: list[str] = [
    "#D97757",  # Orange (Akzent)
    "#94A475",  # Olivgrün
    "#C9A66B",  # Sand-Gold
    # War #C26851 (Terracotta) und lag damit nur dE 8.6 von --accent-primary
    # (#D97757) entfernt — exakt auf der Untergrenze, die sich die Palette
    # selbst gibt. Im Vermoegens-Verlauf zeichnet die Gesamtlinie den Akzent;
    # in der Legende standen beide als 16x3px-Muster nebeneinander, und dort
    # gibt es weder die doppelte Strichstaerke noch die Kontur in Kartenfarbe,
    # die auf der Kurve helfen. Wer die Zuordnung Muster→Konto suchte, fand
    # zwei gleiche Farben. Der Farbton wandert deshalb von 41 auf 20 Grad in
    # den freien roten Rand der Palette (alle anderen liegen zwischen 44 und
    # 159 Grad); Helligkeit (L* 54.0 → 53.9), Saettigung (C* 44.4 → 45.6) und
    # Kartenkontrast (4.02:1) bleiben, wo sie waren. dE zum Akzent: 21.1.
    "#CA6068",  # Altrot
    "#B07D4F",  # Karamell
    "#8C8A6B",  # Khaki
    "#A86B57",  # Rost
    "#7E9C8B",  # Salbei
]

# Nord: die kanonischen Frost-/Aurora-Töne des Nord-Schemas. Bewusst NICHT die
# warme Palette eingefärbt, sondern die Originalfarben — sonst bliebe es ein
# warmes Diagramm in einem kühlen Theme.
CHART_COLORS_NORD: list[str] = [
    "#88C0D0",  # Frost-Cyan
    "#A3BE8C",  # Aurora-Grün
    "#EBCB8B",  # Aurora-Gelb
    "#81A1C1",  # Frost-Blau
    "#B48EAD",  # Aurora-Lila
    "#D08770",  # Aurora-Orange
    # War Nord10 (#5E81AC) und damit der VIERTE Blauton der Palette: 2.50:1
    # gegen die Karte, und beim blossen Aufhellen waere er in Nord9 (chart-3)
    # gelaufen — dE 6.7, schlechter als der heutige Mindestabstand von 9.6.
    # Stattdessen um 30 Grad ins Blauviolett gedreht, innerhalb von Nords
    # Helligkeits- und Saettigungsband: 3.06:1 bei dE 30.0.
    "#8A85D5",  # Frost-Dunkelblau
    "#8FBCBB",  # Frost-Türkis
]

# Synthwave '84: Neon-Volltöne. Bewusst gesättigt — in dieser Farbwelt sind
# grelle Segmente Programm, nicht Betriebsunfall.
CHART_COLORS_SYNTHWAVE: list[str] = [
    "#FF7EDB",  # Neon-Pink
    "#72F1B8",  # Mint
    "#FEDE5D",  # Neon-Gelb
    "#36F9F6",  # Cyan
    "#B893CE",  # Flieder
    "#FF8B39",  # Orange
    "#FE4450",  # Rot
    "#848BBD",  # Gedämpftes Blau
]

# Ayu Light: die Original-Akzente sind fuer helle Flaechen gezeichnet und
# erreichten gegen den Kartengrund (#F3F4F5) nur 1.75 bis 2.65:1 — KEINE der
# elf Farben lag ueber 2.7. Als Segmentflaeche ging das durch, als Linie nicht.
# Abgedunkelt bei gleichem Farbton; der engste Abstand steigt von dE 21.1 auf 25.9.
CHART_COLORS_AYU_HELL: list[str] = [
    "#E66406",  # Orange
    "#719700",  # Grün
    "#C47B0E",  # Amber
    "#1E91E3",  # Blau
    "#A37ACC",  # Violett (erfüllte 3:1 bereits)
    "#389B7A",  # Türkis
    "#EE5A5A",  # Rot
    "#2F95B8",  # Hellblau
]

# Melange: gedeckte, warme Erdtöne — derselbe ruhige Charakter wie die Fläche.
CHART_COLORS_MELANGE: list[str] = [
    "#EBB481",  # Sand
    "#85B695",  # Salbei
    "#EBC06D",  # Honig
    "#A3A9CE",  # Staubblau
    "#CF9BC2",  # Altrosa
    "#89B3B6",  # Petrol
    "#D47766",  # Ton
    "#A98A78",  # Taupe
]

# Hell: dieselben Farbtoene wie die Standardpalette, aber abgedunkelt. Auf dem
# hellen Kartengrund (#ECE6DA) erreichten die Originale nur 1.85 bis 2.87:1 —
# als Flaeche noch hinnehmbar, als 2.5px-Linie im Verlaufsdiagramm nicht mehr.
# WCAG 1.4.11 verlangt fuer grafische Objekte, die zum Verstaendnis noetig sind,
# 3:1. Farbton und Saettigung bleiben, nur die Helligkeit wandert; der engste
# Abstand innerhalb der Palette steigt dabei von dE 8.6 auf 11.1.
CHART_COLORS_LIGHT: list[str] = [
    "#D25F3A",  # Orange
    "#79895A",  # Olivgrün
    "#A27C3B",  # Ocker
    "#CA6068",  # Altrot
    "#A9784C",  # Karamell
    "#868467",  # Moos
    "#A86B57",  # Rostbraun
    "#6A8A78",  # Salbei
]

PALETTEN: dict[str, list[str]] = {
    "dark": CHART_COLORS,
    "light": CHART_COLORS_LIGHT,
    "nord": CHART_COLORS_NORD,
    "synthwave": CHART_COLORS_SYNTHWAVE,
    "ayu-hell": CHART_COLORS_AYU_HELL,
    "melange": CHART_COLORS_MELANGE,
}

# Semantische Geldfluss-Farben (Sankey): Einnahmen, Überschuss/Sparen, Defizit.
INCOME_COLOR = "#6E8C5A"
# Kuehl statt oliv: „Sparen" lag als #94A475 nur dE 12.0 von „Einnahmen"
# (#6E8C5A) entfernt — zwei Olivtoene, die im Sankey nebeneinander stehen und
# entgegengesetzte Bedeutung tragen. Jetzt dE 39.2. Der eine kuehle Ton in der
# sonst warmen Palette ist Absicht: er markiert die Grenze zwischen dem, was
# hereinkommt, und dem, was liegen bleibt.
SAVE_COLOR = "#7BA8B8"
RESERVE_COLOR = "#C26851"

# Je Theme: (Einnahmen, Sparen/Überschuss, Reserve/Defizit).
# Die hellen Skins brauchen eigene Werte: die dunklen Varianten der jeweiligen
# Farben verfehlten auf hellem Grund die 3:1 (Aurora-Rot 2.46:1, Ayu-Rot 2.61:1).
_SEMANTIK: dict[str, tuple[str, str, str]] = {
    "light": ("#6D8A59", "#548A9D", "#C26851"),      # Oliv / Petrol / Terrakotta
    "nord": ("#A3BE8C", "#88C0D0", "#C8777F"),      # Aurora-Grün / Frost-Cyan / Aurora-Rot
    "synthwave": ("#72F1B8", "#36F9F6", "#FE4450"),  # Mint / Cyan / Neon-Rot
    "ayu-hell": ("#719700", "#1E91E3", "#EE5A5A"),   # Grün / Blau / Rot
    "melange": ("#85B695", "#A3A9CE", "#D47766"),    # Salbei / Staubblau / Ton
}
_SEMANTIK_STANDARD = (INCOME_COLOR, SAVE_COLOR, RESERVE_COLOR)


def _key(theme: str | None) -> str:
    return (theme or "").strip().lower()


# --------------------------------------------------------------------------
# Ausgabe ins Markup: CSS-Variablen, keine Hex-Werte
# --------------------------------------------------------------------------
#
# Die Hex-Listen oben bleiben die Quelle — aus ihnen erzeugt
# ``scripts/_gen_chart_tokens.py`` den ``--chart-*``-Block je Skin in skins.css,
# und ``tests/test_chart_tokens.py`` rechnet nach, dass beide übereinstimmen.
#
# Warum nicht direkt die Hex-Werte ausliefern? Der Theme-Wechsel läuft im
# Browser: er setzt nur ``data-theme`` am ``<html>``, worauf die CSS-Variablen
# umschalten. Ein server-seitig eingesetzter Hex-Wert kann davon nichts wissen —
# die Diagramme blieben in den Farben stehen, mit denen die Seite geladen wurde,
# und wurden erst beim nächsten Seitenaufbau richtig. Mit ``var(--chart-3)``
# entscheidet der Browser die Farbe im Moment des Zeichnens.
#
# Der Parameter ``theme`` ist dadurch wirkungslos geworden. Er bleibt in der
# Signatur, damit die Aufrufer unverändert bleiben, und weil ein Skin, der
# eigene Diagrammfarben will, sie schlicht in skins.css setzt.

_ZYKLUS = len(CHART_COLORS)


def chart_colors(theme: str | None = None) -> list[str]:
    """Die Diagramm-Palette als CSS-Variablen-Referenzen."""
    return [f"var(--chart-{i})" for i in range(_ZYKLUS)]


def color_at(idx: int, theme: str | None = None) -> str:
    """Vollton an Position ``idx`` (zyklisch), als ``var(--chart-N)``."""
    return f"var(--chart-{idx % _ZYKLUS})"


def icon_color_at(idx: int) -> str:
    """Lesbare Icon-Farbe AUF dem Vollton an Position ``idx``.

    Ersetzt das frühere ``icon_color_for(hex)``: Sobald die Farbe selbst nur
    noch eine Variablen-Referenz ist, lässt sich ihre Helligkeit server-seitig
    nicht mehr bestimmen. Der passende Gegenwert wird deshalb beim Erzeugen der
    Skin-Tokens einmal ausgerechnet und steht als ``--chart-N-on`` daneben.
    """
    return f"var(--chart-{idx % _ZYKLUS}-on)"


def income_color(theme: str | None = None) -> str:
    """Sankey-Einnahmenfarbe."""
    return "var(--chart-income)"


def save_color(theme: str | None = None) -> str:
    """Sankey-Spar-/Überschussfarbe."""
    return "var(--chart-save)"


def reserve_color(theme: str | None = None) -> str:
    """Sankey-Reserve-/Defizitfarbe."""
    return "var(--chart-reserve)"


def icon_color_for(hex_color: str) -> str:
    """Gut lesbare Icon-Farbe (dunkel oder weiss) je nach Helligkeit des Segments.

    Damit das Icon im farbigen Balken immer lesbar bleibt — unabhängig davon,
    welche Palette gerade gilt.
    """
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return "#FFFFFF"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255  # wahrgenommene Helligkeit (Rec. 601)
    return "#1F1E1B" if lum > 0.62 else "#FFFFFF"
