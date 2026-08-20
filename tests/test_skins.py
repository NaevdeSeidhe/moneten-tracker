"""Prüft den Token-Vertrag aus ``static/css/skins.css`` maschinell.

Warum als Test und nicht als Kommentar: In ``nord`` stand jahrelang
``--text-tertiary: #9AA3B2;  /* 4.6:1 auf --bg-surface */`` — nachgemessen
waren es 3.96:1. Ein Kommentar behauptet einen Wert, ein Test hält ihn.

Geprüft wird ohne Browser: die Datei wird geparst, die Farben werden nach
WCAG 2.x umgerechnet. Skins, die ein Token nicht setzen, erben den Dark-Wert —
genau so, wie es die Kaskade zur Laufzeit auch tut.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

SKINS_CSS = Path(__file__).parent.parent / "src" / "moneten" / "static" / "css" / "skins.css"

# Diese Token landen als TEXT auf --bg-surface (kleinste Schrift 12px) und
# brauchen deshalb WCAG-AA für Kleintext.
TEXT_TOKENS = (
    "--text-primary",
    "--text-secondary",
    "--text-tertiary",
    "--accent-tertiary",
    "--dusty-rose",
    "--warn",
    "--danger",
)
MIN_KONTRAST = 4.5

# Die drei Ampelstufen müssen als Farben unterscheidbar sein. Luminanz-Kontrast
# taugt dafür NICHT (Bernstein und Rot können gleich hell sein), darum ΔE.
AMPEL = ("--accent-tertiary", "--warn", "--danger")
MIN_DELTA_E = 20.0


def _parse_skins() -> dict[str, dict[str, str]]:
    """Liest ``skins.css`` und liefert ``{skin: {token: farbe}}``.

    Kommentare fliegen zuerst raus: der Dateikopf enthält als Dokumentation ein
    Beispiel ``[data-theme="name"]``, das sonst als echter Skin gezählt würde.
    """
    text = re.sub(r"/\*.*?\*/", "", SKINS_CSS.read_text(encoding="utf-8"), flags=re.S)
    skins: dict[str, dict[str, str]] = {}
    for selektor, block in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        namen = re.findall(r'\[data-theme="([^"]+)"\]', selektor)
        if ":root" in selektor and not namen:
            namen = ["dark"]
        if not namen:
            continue
        token = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))
        for name in namen:
            skins.setdefault(name, {}).update({k: v.strip() for k, v in token.items()})
    return skins


def _rgb(farbe: str) -> tuple[float, float, float]:
    """``#RRGGBB`` oder ``#RGB`` → 0-255-Tripel."""
    h = farbe.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _luminanz(rgb: tuple[float, float, float]) -> float:
    def kanal(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (kanal(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _kontrast(a: str, b: str) -> float:
    l1, l2 = _luminanz(_rgb(a)), _luminanz(_rgb(b))
    hell, dunkel = max(l1, l2), min(l1, l2)
    return (hell + 0.05) / (dunkel + 0.05)


def _lab(farbe: str) -> tuple[float, float, float]:
    def kanal(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (kanal(c) for c in _rgb(farbe))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(v: float) -> float:
        return v ** (1 / 3) if v > 0.008856 else (7.787 * v + 16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _delta_e(a: str, b: str) -> float:
    """CIE76 — grob, aber für „sind das zwei Farben?" völlig ausreichend."""
    la, lb = _lab(a), _lab(b)
    return math.dist(la, lb)


@pytest.fixture(scope="module")
def skins() -> dict[str, dict[str, str]]:
    daten = _parse_skins()
    assert "dark" in daten, "Dark-Skin nicht gefunden — Parser oder Datei kaputt"
    return daten


def _wert(skins: dict[str, dict[str, str]], skin: str, token: str) -> str | None:
    """Token des Skins, sonst der geerbte Dark-Wert. Nicht-Hex wird übersprungen."""
    farbe = skins[skin].get(token) or skins["dark"].get(token)
    return farbe if farbe and farbe.startswith("#") else None


def test_skins_gefunden(skins: dict[str, dict[str, str]]) -> None:
    """Der Parser findet alle im Code registrierten Farb-Skins."""
    from moneten.themes import THEMES

    farb_skins = {t.key for t in THEMES if not t.structural}
    assert farb_skins <= set(skins), (
        f"In themes.py registriert, aber nicht in skins.css: {farb_skins - set(skins)}"
    )


@pytest.mark.parametrize("token", TEXT_TOKENS)
def test_textfarben_erreichen_wcag_aa(skins: dict[str, dict[str, str]], token: str) -> None:
    """Jede Farbe, die als Text auf einer Karte landet, schafft 4.5:1."""
    for skin in skins:
        flaeche = _wert(skins, skin, "--bg-surface")
        farbe = _wert(skins, skin, token)
        if not flaeche or not farbe:
            continue
        k = _kontrast(farbe, flaeche)
        assert k >= MIN_KONTRAST, (
            f"{skin}: {token} ({farbe}) erreicht auf --bg-surface ({flaeche}) "
            f"nur {k:.2f}:1, nötig sind {MIN_KONTRAST}:1"
        )


def test_warnfarbe_liest_sich_auch_auf_dem_belegpapier(skins: dict[str, dict[str, str]]) -> None:
    """Der Beleg-Scan meldet auf dem Papier, dass Positionen ungeprüft sind.

    Das Papier ist die einzige Fläche der App, die NICHT ``--bg-surface`` ist —
    der Vertrag oben deckt sie also nicht ab. Genau dort steht aber der Hinweis,
    dass die Gegenprobe nicht aufgeht (``.kz-warn``, ``.kz.unsicher``). Eine
    Warnung, die man nicht lesen kann, ist keine.
    """
    for skin in skins:
        papier = _wert(skins, skin, "--receipt-bg")
        warn = _wert(skins, skin, "--warn")
        if not papier or not warn:
            continue
        k = _kontrast(warn, papier)
        assert k >= MIN_KONTRAST, (
            f"{skin}: --warn ({warn}) erreicht auf --receipt-bg ({papier}) "
            f"nur {k:.2f}:1, nötig sind {MIN_KONTRAST}:1"
        )


def test_ampelstufen_sind_unterscheidbar(skins: dict[str, dict[str, str]]) -> None:
    """ok / warn / over dürfen nicht dieselbe Farbe in drei Namen sein."""
    for skin in skins:
        for i, a in enumerate(AMPEL):
            for b in AMPEL[i + 1 :]:
                fa, fb = _wert(skins, skin, a), _wert(skins, skin, b)
                if not fa or not fb:
                    continue
                d = _delta_e(fa, fb)
                assert d >= MIN_DELTA_E, (
                    f"{skin}: {a} ({fa}) und {b} ({fb}) liegen mit ΔE {d:.1f} zu nah "
                    f"beieinander — die Ampel hätte faktisch eine Stufe weniger"
                )


def test_warnfarbe_ist_nicht_die_markenfarbe(skins: dict[str, dict[str, str]]) -> None:
    """Sonst liest sich jede Akzentfläche der App als Warnung."""
    for skin in skins:
        warn, akzent = _wert(skins, skin, "--warn"), _wert(skins, skin, "--accent-primary")
        if not warn or not akzent:
            continue
        d = _delta_e(warn, akzent)
        assert d >= MIN_DELTA_E, (
            f"{skin}: --warn ({warn}) ist von --accent-primary ({akzent}) "
            f"kaum zu unterscheiden (ΔE {d:.1f})"
        )


def test_pflichttoken_vollstaendig(skins: dict[str, dict[str, str]]) -> None:
    """Jeder Skin setzt die Flächen- und Textbasis selbst (kein stiller Dark-Erbe).

    Bewusst nur die Basis: alles Weitere darf erben, das ist der Sinn des
    Fallbacks. Fehlt aber eine Fläche, sieht der Skin schlicht falsch aus.
    """
    basis = ("--bg-page", "--bg-surface", "--bg-sunken",
             "--text-primary", "--text-secondary", "--text-tertiary")
    for skin, token in skins.items():
        fehlend = [t for t in basis if t not in token]
        assert not fehlend, f"{skin}: Pflicht-Token fehlen — {fehlend}"


# ---------------------------------------------------------------------------
# Benutzte Token müssen auch existieren
# ---------------------------------------------------------------------------

_STATIC = Path(__file__).resolve().parents[1] / "src" / "moneten" / "static"
_TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "moneten" / "templates"

# var(--x, fallback) ist gültig, auch wenn --x fehlt — der Fallback greift dann.
_VAR_OHNE_FALLBACK = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)", re.IGNORECASE)
_DEFINITION = re.compile(r"(--[a-z0-9-]+)\s*:", re.IGNORECASE)
_SET_PROPERTY = re.compile(r"""setProperty\(\s*['"](--[a-z0-9-]+)['"]""", re.IGNORECASE)


def _alle_dateien() -> list[Path]:
    return [
        *sorted(_STATIC.rglob("*.css")),
        *sorted(_TEMPLATES.rglob("*.html")),
        *sorted(_STATIC.rglob("*.js")),
    ]


def test_jedes_benutzte_token_ist_definiert() -> None:
    """Ein `var(--x)` auf ein nicht existierendes Token scheitert **still**.

    Die ganze Kurzschreibweise wird ungültig — aus `border-top: 1px solid
    var(--border)` wird gar kein Rahmen, aus `stroke="var(--success)"` gar keine
    Linie. Nichts bricht, nichts wird geloggt, es fehlt einfach.

    Genau das ist zweimal passiert: `--success` in der Preisverlauf-Sparkline
    (keine Linie bei billiger gewordenen Artikeln) und `--border` in zwei
    Trennlinien. Beide Male sah die Seite funktionsfähig aus.
    """
    # Definitionen stehen nicht nur im CSS: manche Token setzt erst das Template
    # per Inline-Style (`style="--ziel: 40"` für die Ziellinie der Bar-Quote) oder
    # das JS per setProperty. Beides ist gewollt — der Wert ist dann dynamisch.
    definiert: set[str] = set()
    for pfad in _alle_dateien():
        text = pfad.read_text(encoding="utf-8")
        definiert |= set(_DEFINITION.findall(text))
        definiert |= set(_SET_PROPERTY.findall(text))

    fehler: list[str] = []
    for pfad in _alle_dateien():
        text = pfad.read_text(encoding="utf-8")
        for nr, zeile in enumerate(text.splitlines(), 1):
            for token in _VAR_OHNE_FALLBACK.findall(zeile):
                if token not in definiert:
                    fehler.append(f"{pfad.name}:{nr} benutzt {token}")

    assert not fehler, (
        "Diese CSS-Variablen werden benutzt, aber nirgends definiert:\n  "
        + "\n  ".join(fehler)
    )
