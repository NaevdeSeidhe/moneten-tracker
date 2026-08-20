"""Tests für den Squarified-Treemap-Layouter (services/treemap.py)."""

from __future__ import annotations

from decimal import Decimal

from moneten.services.treemap import build_treemap


def test_empty_returns_empty():
    assert build_treemap([]) == []
    # Nur Null-/Negativbeträge → nichts zu zeigen.
    assert build_treemap([("X", Decimal("0"), "tag"), ("Y", Decimal("-5"), "tag")]) == []


def test_tiles_sorted_desc_and_complete():
    items = [
        ("Klein", Decimal("50"), "a"),
        ("Gross", Decimal("500"), "b"),
        ("Mittel", Decimal("200"), "c"),
    ]
    tiles = build_treemap(items)
    assert len(tiles) == 3
    # Absteigend nach Betrag sortiert (grösste Kachel zuerst).
    assert [t["label"] for t in tiles] == ["Gross", "Mittel", "Klein"]
    # Jede Kachel hat Koordinaten, Farbe, Anteil.
    for t in tiles:
        assert {"x", "y", "w", "h", "color", "pct"} <= set(t.keys())


def test_area_proportional_to_amount():
    """Die Fläche der grössten Kachel muss klar grösser sein als die der kleinsten."""
    tiles = build_treemap([("A", Decimal("800"), "i"), ("B", Decimal("100"), "i")])
    areas = {t["label"]: t["w"] * t["h"] for t in tiles}
    assert areas["A"] > areas["B"] * 5  # ~8x mehr Betrag → deutlich mehr Fläche


def test_tiles_within_bounds():
    tiles = build_treemap([("A", Decimal("3"), "i"), ("B", Decimal("2"), "i"), ("C", Decimal("1"), "i")])
    for t in tiles:
        assert t["x"] >= -0.01 and t["y"] >= -0.01
        assert t["x"] + t["w"] <= 100.01
        assert t["y"] + t["h"] <= 100.01


# ---------------------------------------------------------------------------
# Kleinstbeträge: eine Kachel, die nichts mehr tragen kann, ist keine
# ---------------------------------------------------------------------------


def test_kleinstbetrag_bekommt_gar_keine_kachel():
    """Neben einem grossen Posten blieb von der Kachel nur ihr Innenabstand.

    Gemessen im Browser: ein Kleinbetrag neben einem grossen bekam eine Kachel
    ohne jede Inhaltsfläche — sichtbar waren die 3px Polster links und rechts,
    das Namensfeld war 0px breit, und ``innerText`` übersprang sie ganz. Auf dem
    Bildschirm ein farbiger Strich an der Kartenkante: er las sich als
    abgeschnittene Grafik, nicht als Datenpunkt. Die Karte zeigt die GRÖSSTEN
    Ausgaben und schneidet ohnehin nach Rang ab — was unter einem Prozent liegt,
    ist keine.
    """
    tiles = build_treemap([
        ("Wohnen", Decimal("1400"), "home"),
        ("Essen", Decimal("600"), "cart"),
        ("Kiosk", Decimal("0.05"), "tag"),
    ])
    assert [t["label"] for t in tiles] == ["Wohnen", "Essen"]


def test_anteil_zaehlt_die_weggelassenen_posten_mit():
    """Sonst behauptete die letzte verbliebene Kachel 100 %.

    Fläche und Anteil haben verschiedene Nenner: die Flächen normieren auf die
    gezeigten Kacheln (sonst bliebe ein Loch in der Karte), der Anteil auf
    alles. Ein Posten, der 94 % ausmacht, darf im Tooltip nicht als das Ganze
    dastehen, nur weil der Rest zu klein zum Zeichnen war.
    """
    tiles = build_treemap(
        [("Gross", Decimal("5000"), "i")]
        + [(f"Klein {i}", Decimal("50"), "i") for i in range(6)]
    )
    assert len(tiles) == 1, "Vorbedingung: die kleinen Posten fallen weg"
    assert tiles[0]["pct"] < 100


def test_betrag_geht_unveraendert_durch():
    """Der Betrag wird angezeigt, nicht gerechnet — er muss ankommen wie er kam.

    Der Weg über ``float`` und ein anschliessendes ``quantize`` gab ihn neu
    gerundet zurück, und zwar zur geraden Ziffer statt kaufmännisch wie überall
    sonst im Projekt. Die Kachel ist der letzte Halt vor dem Bildschirm; hier
    darf ein Betrag keine Darstellung mehr durchlaufen, die ihn nicht halten muss.
    """
    tiles = build_treemap([("Wohnen", Decimal("5000.005"), "home")])
    assert tiles[0]["value"] == Decimal("5000.005")
