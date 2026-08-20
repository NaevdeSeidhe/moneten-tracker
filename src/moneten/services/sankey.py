"""Sankey-Geldfluss-Diagramm — Knoten-Layout + Bézier-Bänder (reines SVG, offline).

Drei Spalten: Einnahmequellen → zentraler „Budget"-Knoten → Ausgaben-Kategorien
(plus „Überschuss · Sparen" bzw. „aus Reserve", damit beide Seiten bilanzieren).
Eine einzige Skala (px je CHF) sorgt für überall gleich breite Bänder.

Reine Funktion ``build_flow(income_items, expense_items)`` → Render-Modell für
das Template. Keine DB, keine Seiteneffekte — gut testbar.
"""

from __future__ import annotations

from decimal import Decimal

# Zentrale Chart-Palette (eine Quelle für Donut/Sankey/Treemap/Budget) — theme-bewusst.
from moneten.palette import chart_colors, income_color, reserve_color, save_color

# Geometrie (SVG-Einheiten). Die Label-Reserven links/rechts müssen breit genug
# sein, sonst werden lange Beschriftungen ("Übrige Ausgaben · CHF 1'234.50")
# am viewBox-Rand abgeschnitten.
_NODE_W = 16.0
_GAP = 16.0
_PAD = 22.0
_TARGET_H = 320.0      # Pixelhöhe, auf die die Gesamtsumme abgebildet wird
_LEFT_X = 290.0        # x der linken Knoten-Säule (links davon: Labels)
_RIGHT_LABEL = 330.0   # Label-Platz rechts (lange Labels wie „Überschuss · Sparen · CHF …")
_WIDTH = 1200.0        # genug Breite für beide Label-Spalten + Flussbereich

#: Mindestabstand zweier Beschriftungen derselben Spalte, in SVG-Einheiten.
#:
#: Ein kleiner Posten ist neben einer grossen Gesamtsumme keine 2 Einheiten
#: hoch — bei einem Verhältnis von etwa eins zu zweihundert bleibt ein Strich. Sass
#: die Beschriftung auf seiner Mitte, überlappte sie die des Nachbarn — im
#: Browser nachgemessen sogar bei Massstab 1.0, wo die Schrift am kleinsten
#: ausfällt. Verschoben wird NUR der Text; Balken und Bänder bleiben exakt dort,
#: wo der Betrag sie hinstellt, sonst löge das Diagramm.
#:
#: 26 Einheiten ≈ 13px Schrift × 1.35 Zeilenhöhe bei Massstab 0.75 — dem
#: kleinsten Massstab, den die Karte nach der Mobile-Regel unten noch zulässt.
_LABEL_MIN_GAP = 26.0


def entzerre_labels(mitten: list[float], hoehe: float, *, abstand: float = _LABEL_MIN_GAP,
                    von: float = 0.0, bis: float | None = None) -> list[float]:
    """Schiebt Beschriftungs-Höhen auseinander, bis keine zwei sich berühren.

    ``mitten`` sind die Knotenmitten von oben nach unten. Zurück kommen die
    y-Werte für den TEXT — die Reihenfolge bleibt, jeder Nachbar hat mindestens
    ``abstand``, und der ganze Block bleibt zwischen ``von`` und ``bis``.

    Zwei Durchgänge: erst von oben nach unten drücken, dann von unten nach oben
    zurück. Der zweite ist nötig, weil der erste den Block nach unten aus dem
    Bild schieben kann — dann muss er wieder hoch, und zwar geschlossen.
    """
    if not mitten:
        return []
    if bis is None:
        bis = hoehe
    y = list(mitten)
    for i in range(1, len(y)):
        y[i] = max(y[i], y[i - 1] + abstand)
    # Unten anschlagen und rückwärts zurückdrücken.
    y[-1] = min(y[-1], bis)
    for i in range(len(y) - 2, -1, -1):
        y[i] = min(y[i], y[i + 1] - abstand)
    # Oben anschlagen. Passt der Block gar nicht mehr, gewinnt der Abstand: ein
    # Label am Rand ist lesbar, zwei uebereinander sind es nicht.
    y[0] = max(y[0], von)
    for i in range(1, len(y)):
        y[i] = max(y[i], y[i - 1] + abstand)
    return y


def _ribbon(sx: float, sy0: float, sy1: float, tx: float, ty0: float, ty1: float) -> str:
    """Gefülltes Band von Quell-Kante (sx, sy0..sy1) zu Ziel-Kante (tx, ty0..ty1)."""
    mx = (sx + tx) / 2
    return (
        f"M {round(sx, 1)},{round(sy0, 1)} "
        f"C {round(mx, 1)},{round(sy0, 1)} {round(mx, 1)},{round(ty0, 1)} {round(tx, 1)},{round(ty0, 1)} "
        f"L {round(tx, 1)},{round(ty1, 1)} "
        f"C {round(mx, 1)},{round(ty1, 1)} {round(mx, 1)},{round(sy1, 1)} {round(sx, 1)},{round(sy1, 1)} Z"
    )


def build_flow(
    income_items: list[tuple[str, Decimal]],
    expense_items: list[tuple[str, Decimal]],
    *,
    theme: str | None = None,
) -> dict | None:
    """Baut das Render-Modell des Geldfluss-Diagramms.

    ``income_items`` / ``expense_items``: (Label, Betrag) — Beträge positiv.
    Liefert ``None``, wenn es nichts zu zeigen gibt.
    """
    income = [(lbl, Decimal(a)) for lbl, a in income_items if a and a > 0]
    expense = [(lbl, Decimal(a)) for lbl, a in expense_items if a and a > 0]
    total_in = sum((a for _, a in income), Decimal("0"))
    total_out = sum((a for _, a in expense), Decimal("0"))
    if total_in <= 0 and total_out <= 0:
        return None

    left = list(income)
    right = list(expense)
    if total_in >= total_out:
        surplus = total_in - total_out
        if surplus > 0:
            right = right + [("Überschuss · Sparen", surplus)]
        total = total_in
    else:
        right = right
        left = left + [("aus Reserve", total_out - total_in)]
        total = total_out
    if total <= 0:
        return None

    px = _TARGET_H / float(total)
    n_max = max(len(left), len(right), 1)
    svg_h = _TARGET_H + _GAP * (n_max - 1) + 2 * _PAD

    right_x = _WIDTH - _RIGHT_LABEL - _NODE_W
    hub_x = (_WIDTH - _NODE_W) / 2

    palette = chart_colors(theme)

    def _column(items: list[tuple[str, Decimal]], x: float, side: str, base_color=None) -> list[dict]:
        heights = [float(a) * px for _, a in items]
        block = sum(heights) + _GAP * (len(items) - 1)
        y = (svg_h - block) / 2
        out = []
        for i, ((label, amount), h) in enumerate(zip(items, heights, strict=True)):
            if label == "Überschuss · Sparen":
                color = save_color(theme)
            elif label == "aus Reserve":
                color = reserve_color(theme)
            elif base_color:
                color = base_color
            else:
                color = palette[i % len(palette)]
            out.append({
                "label": label, "amount": amount, "x": x, "y0": y, "y1": y + h,
                "h": h, "mid": y + h / 2, "color": color, "side": side,
            })
            y += h + _GAP
        return out

    left_nodes = _column(left, _LEFT_X, "left", base_color=income_color(theme))
    right_nodes = _column(right, right_x, "right")
    # Beschriftung eigenständig entzerren — der Balken bleibt auf seiner Mitte,
    # nur der Text weicht aus. Ohne das lagen die Labels kleiner Kategorien
    # uebereinander (nachgemessen: 8 Paare bei 375 px, eines noch bei 1280 px).
    for spalte in (left_nodes, right_nodes):
        for knoten, y in zip(
            spalte,
            entzerre_labels([n["mid"] for n in spalte], svg_h, von=_PAD, bis=svg_h - _PAD),
            strict=True,
        ):
            knoten["label_y"] = round(y, 1)
    hub_h = float(total) * px
    hub_y = (svg_h - hub_h) / 2
    hub = {"label": "Budget", "amount": total, "x": hub_x, "y0": hub_y, "y1": hub_y + hub_h,
           "h": hub_h, "mid": hub_y + hub_h / 2}

    links: list[dict] = []
    # Einnahmen → Hub (links stapeln sich am Hub-Eingang von oben).
    hub_in = hub_y
    for n in left_nodes:
        sx = n["x"] + _NODE_W
        links.append({"path": _ribbon(sx, n["y0"], n["y1"], hub_x, hub_in, hub_in + n["h"]),
                      "color": n["color"]})
        hub_in += n["h"]
    # Hub → Ausgaben (Ausgang von oben).
    hub_out = hub_y
    hub_rx = hub_x + _NODE_W
    for n in right_nodes:
        links.append({"path": _ribbon(hub_rx, hub_out, hub_out + n["h"], n["x"], n["y0"], n["y1"]),
                      "color": n["color"]})
        hub_out += n["h"]

    return {
        "width": _WIDTH, "height": round(svg_h, 1), "node_w": _NODE_W,
        "left": left_nodes, "right": right_nodes, "hub": hub, "links": links,
        "total": total,
    }
