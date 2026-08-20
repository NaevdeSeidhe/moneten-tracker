"""Rundet die Verlaufskurve, ohne Werte zu erfinden?

Eine geglättete Linie sieht besser aus als ein Polygonzug — aber sie darf nichts
behaupten. Zwischen zwei Messwerten liegt keine Information; eine Kurve, die
dort über den höheren oder unter den tieferen ausschlägt, zeigt einen Betrag,
den es nie gab.

Der zweite Fall ist auf der Verlaufsseite noch dringlicher: die x-Achse ist
echte Zeit, und zwischen zwei Punkten liegt mal ein Monat, mal ein Jahr. Bei so
ungleichen Abständen wandert der Kontrollpunkt einer Catmull-Rom-Kurve hinter
seinen Vorgänger zurück — die Linie schlägt eine sichtbare Schlaufe.

Beide Fehler sind hier nachgemessen, nicht behauptet: die Tests tasten die
fertigen Bézier-Segmente ab und prüfen die tatsächliche Kurvenform.
"""

from __future__ import annotations

import re

import pytest

from moneten.services.charts import curve_path

# Feiner als jede Bildschirmauflösung — ein Ausschlag, der hier durchrutscht,
# ist auch keiner mehr.
SCHRITTE = 100


def _abtasten(pfad: str) -> tuple[list[float], list[float]]:
    """Fährt den Pfad ab und gibt alle x- und y-Werte der Kurve zurück.

    Kubische Bézier nach der Standardformel; die Zahlen kommen aus dem
    ``d``-Attribut selbst. Damit wird geprüft, was gezeichnet WIRD — nicht,
    was die Kontrollpunkte vermuten lassen.
    """
    zahlen = [float(z) for z in re.findall(r"-?\d+\.?\d*", pfad)]
    xs: list[float] = []
    ys: list[float] = []
    p = (zahlen[0], zahlen[1])
    i = 2
    while i + 5 < len(zahlen):
        c1 = (zahlen[i], zahlen[i + 1])
        c2 = (zahlen[i + 2], zahlen[i + 3])
        e = (zahlen[i + 4], zahlen[i + 5])
        for k in range(SCHRITTE + 1):
            t = k / SCHRITTE
            u = 1 - t
            xs.append(u**3 * p[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * e[0])
            ys.append(u**3 * p[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * e[1])
        p = e
        i += 6
    return xs, ys


# (Name, Punkte) — Formen, bei denen eine ungeklemmte Spline nachweislich
# über die Messwerte hinausschiesst.
AUSSCHLAG_FAELLE = [
    ("einzelner Ausreisser", [(0.0, 50.0), (1.0, 50.0), (2.0, 10.0), (3.0, 50.0), (4.0, 50.0)]),
    ("Stufe", [(0.0, 80.0), (1.0, 80.0), (2.0, 20.0), (3.0, 20.0)]),
]


@pytest.mark.parametrize(("name", "pts"), AUSSCHLAG_FAELLE)
def test_geklemmte_kurve_verlaesst_den_wertebereich_nicht(
    name: str, pts: list[tuple[float, float]]
) -> None:
    """Die Kurve bleibt zwischen kleinstem und grösstem echten Wert."""
    lo_soll = min(y for _, y in pts)
    hi_soll = max(y for _, y in pts)
    _, ys = _abtasten(curve_path(pts, klemmen=True))

    assert min(ys) >= lo_soll - 0.01, f"{name}: unterschreitet den kleinsten Messwert"
    assert max(ys) <= hi_soll + 0.01, f"{name}: überschreitet den grössten Messwert"


@pytest.mark.parametrize(("name", "pts"), AUSSCHLAG_FAELLE)
def test_ungeklemmt_schiesst_wirklich_ueber(
    name: str, pts: list[tuple[float, float]]
) -> None:
    """Gegenprobe: ohne Klemme tritt der Fehler tatsächlich auf.

    Ohne diesen Test wäre nicht belegt, dass die Klemme überhaupt etwas
    verhindert — sie könnte genauso gut wirkungslos sein und der Test darüber
    trotzdem grün.
    """
    lo_soll = min(y for _, y in pts)
    hi_soll = max(y for _, y in pts)
    _, ys = _abtasten(curve_path(pts, klemmen=False))

    assert min(ys) < lo_soll - 0.1 or max(ys) > hi_soll + 0.1, (
        f"{name}: erwarteter Ausschlag blieb aus — der Fall taugt nicht als Beleg"
    )


def test_geklemmte_kurve_laeuft_nie_rueckwaerts() -> None:
    """Bei sehr ungleichen Zeitabständen darf die Linie keine Schlaufe schlagen.

    Genau der gemessene Fall: drei Monatswerte dicht beieinander, dann ein Jahr Lücke.
    """
    pts = [(0.0, 60.0), (2.0, 55.0), (4.0, 58.0), (100.0, 20.0)]
    xs, _ = _abtasten(curve_path(pts, klemmen=True))

    rueckwaerts = [(a, b) for a, b in zip(xs, xs[1:], strict=False) if b < a - 1e-9]
    assert not rueckwaerts, f"{len(rueckwaerts)} Rückwärtsschritte in x"


def test_ungeklemmt_laeuft_bei_luecken_rueckwaerts() -> None:
    """Gegenprobe zur Schlaufe — sonst belegt der Test darüber nichts."""
    pts = [(0.0, 60.0), (2.0, 55.0), (4.0, 58.0), (100.0, 20.0)]
    xs, _ = _abtasten(curve_path(pts, klemmen=False))

    assert any(b < a - 1e-9 for a, b in zip(xs, xs[1:], strict=False))


def test_kurve_trifft_jeden_messpunkt() -> None:
    """Glättung verschiebt die Stützstellen nicht.

    Eine Spline, die ihre eigenen Punkte verfehlte, wäre als Datendarstellung
    wertlos — egal wie schön sie aussieht.
    """
    pts = [(0.0, 10.0), (25.0, 40.0), (60.0, 15.0), (100.0, 90.0)]
    pfad = curve_path(pts, klemmen=True)

    assert pfad.startswith("M 0.0,10.0")
    # Jedes C-Segment endet auf seinem Stützpunkt.
    enden = [seg.strip().split()[-1] for seg in pfad.split("C")[1:]]
    assert enden == ["25.0,40.0", "60.0,15.0", "100.0,90.0"]


def test_einzelner_punkt_ergibt_keine_linie() -> None:
    """Durch einen Punkt gibt es keine Linie — nur den Startbefehl.

    Ein erfundenes Segment würde eine Entwicklung zeigen, wo genau eine
    Beobachtung vorliegt.
    """
    assert curve_path([(5.0, 5.0)], klemmen=True) == "M 5.0,5.0"
    assert "C" not in curve_path([(5.0, 5.0)], klemmen=True)


def test_leere_reihe_ergibt_leeren_pfad() -> None:
    """Kein Punkt, kein Pfad — und kein Absturz beim Zeichnen."""
    assert curve_path([], klemmen=True) == ""


def test_klemmen_ist_standardmaessig_aus() -> None:
    """Die bestehenden Sparklines dürfen sich durch die Neuerung nicht ändern.

    Übersicht und Konten zeichnen seit Langem mit derselben Funktion; ein
    geänderter Standardwert hätte ihr Aussehen still mitverändert.
    """
    pts = [(0.0, 80.0), (1.0, 80.0), (2.0, 20.0), (3.0, 20.0)]
    assert curve_path(pts) == curve_path(pts, klemmen=False)
    assert curve_path(pts) != curve_path(pts, klemmen=True)
