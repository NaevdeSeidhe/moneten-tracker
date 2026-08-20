"""Squarified Treemap — Kachel-Layout für „Grösste Ausgaben" (offline, rein Python).

Liefert für eine Liste (Label, Betrag, Icon) Rechtecke, deren **Fläche dem Betrag
entspricht** und die möglichst quadratisch sind (squarified treemap nach
Bruls/Huizing/van Wijk). Koordinaten in Prozent (0–100) → das Template platziert
absolute Divs, CSS animiert sie rein. Keine DB, keine Seiteneffekte → testbar.
"""

from __future__ import annotations

from decimal import Decimal

# Zentrale Chart-Palette (eine Quelle für Donut/Sankey/Treemap/Budget).
from moneten.palette import chart_colors


def _layout_row(sizes: list[float], x: float, y: float, dx: float, dy: float) -> list[dict]:
    """Legt eine Reihe nebeneinander (waagrecht) bzw. übereinander (senkrecht)."""
    covered = sum(sizes)
    rects: list[dict] = []
    if dx >= dy:  # breite Fläche → Spalte (Kacheln untereinander)
        width = covered / dy if dy else 0
        cy = y
        for s in sizes:
            h = s / width if width else 0
            rects.append({"x": x, "y": cy, "w": width, "h": h})
            cy += h
    else:  # hohe Fläche → Reihe (Kacheln nebeneinander)
        height = covered / dx if dx else 0
        cx = x
        for s in sizes:
            w = s / height if height else 0
            rects.append({"x": cx, "y": y, "w": w, "h": height})
            cx += w
    return rects


def _leftover(sizes: list[float], x: float, y: float, dx: float, dy: float) -> tuple[float, float, float, float]:
    """Restfläche nach dem Platzieren einer Reihe."""
    covered = sum(sizes)
    if dx >= dy:
        width = covered / dy if dy else 0
        return (x + width, y, dx - width, dy)
    height = covered / dx if dx else 0
    return (x, y + height, dx, dy - height)


def _worst(sizes: list[float], x: float, y: float, dx: float, dy: float) -> float:
    """Schlechtestes (höchstes) Seitenverhältnis einer Reihe — kleiner ist besser."""
    rects = _layout_row(sizes, x, y, dx, dy)
    worst = 1.0
    for r in rects:
        w, h = r["w"], r["h"]
        if w <= 0 or h <= 0:
            return float("inf")
        worst = max(worst, w / h, h / w)
    return worst


def _squarify(sizes: list[float], x: float, y: float, dx: float, dy: float) -> list[dict]:
    if not sizes:
        return []
    if len(sizes) == 1 or dx <= 0 or dy <= 0:
        return _layout_row(sizes, x, y, dx, dy)
    i = 1
    while i < len(sizes) and _worst(sizes[:i], x, y, dx, dy) >= _worst(sizes[: i + 1], x, y, dx, dy):
        i += 1
    current, remaining = sizes[:i], sizes[i:]
    lx, ly, ldx, ldy = _leftover(current, x, y, dx, dy)
    return _layout_row(current, x, y, dx, dy) + _squarify(remaining, lx, ly, ldx, ldy)


#: Unter diesem Anteil an der Gesamtsumme wird ein Posten nicht gezeichnet.
#: Ohne die Grenze bekam ein Kleinstbetrag neben einem grossen eine Kachel ohne
#: jede Inhaltsfläche: sichtbar blieben nur die 3px Innenabstand links und rechts
#: — ein schmaler Streifen an der Kartenkante, dessen Namensfeld 0px breit ist,
#: den `innerText` überspringt und dessen Farbe wie eine abgeschnittene Grafik
#: aussieht. Die Karte zeigt ohnehin nur die GRÖSSTEN Ausgaben (der Aufrufer
#: schneidet nach Rang ab); was unter einem Prozent liegt, ist keine.
_MIN_ANTEIL = Decimal("0.01")


def build_treemap(items: list[tuple[str, Decimal, str]], width: float = 100.0, height: float = 100.0,
                  *, theme: str | None = None, min_anteil: Decimal = _MIN_ANTEIL) -> list[dict]:
    """Baut das Treemap-Render-Modell.

    ``items``: (Label, Betrag>0, Icon). Sortiert absteigend nach Betrag.
    Rückgabe: Liste von Kacheln mit Prozent-Koordinaten (x/y/w/h), Farbe, Anteil.
    Posten unter ``min_anteil`` der Gesamtsumme werden weggelassen (siehe dort).
    """
    clean = [(lbl, betrag, icon) for lbl, betrag, icon in items if betrag and betrag > 0]
    if not clean:
        return []
    clean.sort(key=lambda t: t[1], reverse=True)
    # Anteil und Fläche haben mit Absicht VERSCHIEDENE Nenner. Der Anteil zählt
    # die weggelassenen Posten mit — sonst stünde bei einer einzigen gezeigten
    # Kachel „100 %", obwohl daneben noch etwas liegt. Die Flächen normieren auf
    # die gezeigten, sonst bliebe in der Karte ein unerklärtes Loch.
    gesamt = sum((b for _, b, _ in clean), Decimal("0"))
    gezeigt = [(lbl, b, icon) for lbl, b, icon in clean if b / gesamt >= min_anteil]
    if not gezeigt:
        return []
    gezeigt_summe = sum((b for _, b, _ in gezeigt), Decimal("0"))

    # Ab hier Geometrie und nicht Geld: die Koordinaten sind Prozentangaben der
    # Karte, die Umrechnung nach Fliesskomma passiert genau einmal und nur für
    # sie. Der Betrag selbst geht unverändert als Decimal in die Kachel — er
    # wird angezeigt, und ein Umweg über float hat in einem Betrag nichts
    # verloren.
    total_area = width * height
    sizes = [float(b / gezeigt_summe) * total_area for _, b, _ in gezeigt]

    rects = _squarify(sizes, 0.0, 0.0, width, height)
    pal = chart_colors(theme)
    out: list[dict] = []
    for idx, ((label, betrag, icon), r) in enumerate(zip(gezeigt, rects, strict=False)):
        out.append({
            "label": label,
            "value": betrag,
            "icon": icon,
            "color": pal[idx % len(pal)],
            "pct": round(betrag / gesamt * 100, 1),
            "x": round(r["x"], 3),
            "y": round(r["y"], 3),
            "w": round(r["w"], 3),
            "h": round(r["h"], 3),
        })
    return out
