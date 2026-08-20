"""Wiederverwendbare SVG-Diagramm-Geometrie (offline, kein Fremd-Tool).

Reine Funktionen zur Berechnung von Pfaden für Sparklines / Flächencharts —
genutzt von Dashboard (Mini-Sparklines) und Konten (Vermögens-Verlauf).
"""

from __future__ import annotations

from decimal import Decimal


def _klemme(wert: float, a: float, b: float) -> float:
    """Hält ``wert`` zwischen ``a`` und ``b``, unabhängig von deren Reihenfolge."""
    lo, hi = (a, b) if a <= b else (b, a)
    return max(lo, min(hi, wert))


def curve_segments(
    pts: list[tuple[float, float]], *, klemmen: bool = False
) -> list[str]:
    """Catmull-Rom-Spline → kubische Bézier-Segmente (weiche, runde Linie).

    ``klemmen`` hält die Kontrollpunkte im Rechteck zwischen den beiden
    Segment-Enden. Eine Bézier-Kurve bleibt immer in der konvexen Hülle ihrer
    Kontrollpunkte — geklemmt kann sie damit weder über den höchsten noch unter
    den tiefsten der beiden Messwerte ausschlagen.

    Das ist bei ungleichen Abständen keine Feinheit, sondern Notwendigkeit. Auf
    der Verlaufsseite liegen zwischen zwei Punkten mal ein Monat, mal ein Jahr.
    Ungeklemmt wandert der Kontrollpunkt dann in x-Richtung hinter seinen
    Vorgänger zurück, und die Linie schlägt eine sichtbare Schlaufe. In
    y-Richtung wäre es schlimmer als hässlich: die Kurve würde zwischen zwei
    Messwerten Beträge durchlaufen, die es nie gab.

    Standardmässig aus, damit die bestehenden Sparklines unverändert aussehen.
    """
    segs: list[str] = []
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[0]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else pts[-1]
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = p2[1] - (p3[1] - p1[1]) / 6
        if klemmen:
            c1x = _klemme(c1x, p1[0], p2[0])
            c2x = _klemme(c2x, p1[0], p2[0])
            c1y = _klemme(c1y, p1[1], p2[1])
            c2y = _klemme(c2y, p1[1], p2[1])
        segs.append(f"C {round(c1x, 2)},{round(c1y, 2)} {round(c2x, 2)},{round(c2y, 2)} {p2[0]},{p2[1]}")
    return segs


def curve_path(pts: list[tuple[float, float]], *, klemmen: bool = False) -> str:
    """Fertiges ``d``-Attribut für eine weiche Linie durch ``pts``.

    Ein einzelner Punkt ergibt nur ein ``M`` — ohne Segment zeichnet SVG nichts,
    was hier richtig ist: eine Linie durch einen Punkt gibt es nicht. Sichtbar
    bleibt er über die Punktmarkierung.
    """
    if not pts:
        return ""
    start = f"M {pts[0][0]},{pts[0][1]}"
    if len(pts) == 1:
        return start
    return start + " " + " ".join(curve_segments(pts, klemmen=klemmen))


def sparkline(values: list[Decimal], w: float = 116, h: float = 34, pad: float = 4) -> dict:
    """Geometrie für eine **weich gerundete** Linie/Fläche (reines SVG).

    Liefert ``line`` (geglätteter Pfad), ``area`` (gefüllte Fläche darunter), den
    letzten Punkt (Markierungs-Dot), ``flat`` (alle gleich) und ``pts`` (Rohpunkte).
    Skaliert über ``w``/``h`` von der Mini-Sparkline bis zum grossen Flächenchart.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return {"line": "", "area": "", "last_x": 0, "last_y": h / 2, "flat": True, "pts": []}
    lo, hi = min(vals), max(vals)
    span = hi - lo
    flat = span == 0
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0
    pts: list[tuple[float, float]] = []
    for i, v in enumerate(vals):
        x = pad + i * step
        y = h / 2 if flat else (h - pad) - (v - lo) / span * (h - 2 * pad)
        pts.append((round(x, 2), round(y, 2)))

    base = h - pad
    if n == 1:
        line = f"M {pts[0][0]},{pts[0][1]}"
        area = ""
    else:
        segs = " ".join(curve_segments(pts))
        line = f"M {pts[0][0]},{pts[0][1]} {segs}"
        area = f"M {pts[0][0]},{base} L {pts[0][0]},{pts[0][1]} {segs} L {pts[-1][0]},{base} Z"
    return {"line": line, "area": area, "last_x": pts[-1][0], "last_y": pts[-1][1], "flat": flat, "pts": pts}
