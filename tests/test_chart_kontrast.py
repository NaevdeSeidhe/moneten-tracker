"""Sind die Diagrammfarben in jedem Skin gegen die Karte zu sehen?

Die Palette war ursprünglich als **Flächenfarbe** gedacht — Segmente und Balken
brauchen keine 4.5:1, und so steht es bis heute im Docstring von
``moneten.palette``. Mit der Verlaufsseite kam ein zweiter Verwendungszweck dazu:
eine 2.5px-Linie. Dafür verlangt WCAG 1.4.11 **3:1** für grafische Objekte, die
zum Verständnis nötig sind, und eine Kurve ist genau das.

Gemessen wurde vor der Korrektur: 19 von 66 Farb/Skin-Paaren lagen darunter, in
``ayu-hell`` lag KEINE der elf Farben über 2.7:1. Dieser Test hält den Zustand
danach fest — jede Farbe gegen den Kartengrund IHRES Skins, nicht gegen einen
Durchschnitt.

Der zweite Teil sichert die Unterscheidbarkeit: eine Farbe dunkler zu machen ist
leicht, dabei in die Nachbarfarbe zu laufen auch. Die Zuordnung Linie→Reihe
hängt daran.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moneten.palette import _SEMANTIK, _SEMANTIK_STANDARD, CHART_COLORS, PALETTEN
from moneten.services.account_charts import MAX_KONTO_LINIEN, konto_farbe
from moneten.services.verlauf_positionen import REST_FARBE

# WCAG 1.4.11 für grafische Objekte. Bewusst nicht 4.5:1 — das gilt für Text,
# und diese Farben tragen keinen.
MIN_KONTRAST = 3.0

# Der kleinste Abstand, den die Paletten schon vor der Korrektur einhielten
# (gemessen: dark und light 8.6, nord 9.6). Als Untergrenze übernommen, damit
# eine künftige Korrektur die Unterscheidbarkeit nicht schleichend aufgibt.
#
# Die 8.6 in ``dark`` waren ``chart-0``/``chart-3`` und damit zugleich der
# Abstand der dritten Konto-Linie zur Gesamtlinie — im Bild nicht mehr zu
# trennen. ``chart-3`` liegt seither bei dE 21.1; gemessen sind jetzt dark 13.7,
# light 11.1, nord 9.6. Die Schranke bleibt trotzdem bei 8.5: sie gilt für ALLE
# Skins, und nord hätte bei 9.5 noch 0.1 Luft — eine Zahl, die beim nächsten
# Feinschliff aus Versehen rot wird, sichert nichts.
MIN_ABSTAND = 8.5

# EIGENE Schranke fuer „Palettenfarbe gegen Akzentfarbe". Sie ist eine andere
# Frage als der Abstand der Palettenfarben untereinander und darf sich darum
# nicht dieselbe Konstante teilen — genau daran ist die Absicherung gescheitert:
# das gemeldete Farbpaar (dark, --chart-3 #C26851 gegen den Akzent) mass 8.61
# und lag damit 0.11 UEBER der 8.5-Huerde. Der Test, der genau diesen Fehler
# festnageln sollte, liess ihn durch; ein Ruecksetzen der Farbe machte die Suite
# nicht rot.
# 11 statt 8.5: der kleinste ECHTE Wert ueber alle Skins ist 11.9 (light). Die
# Huerde liegt knapp darunter und nagelt den Ist-Zustand fest, ohne einen Skin
# zu erzwingen, den es nicht gibt. Das gemeldete Paar mit 8.61 faellt durch. Der Abstand der Palettenfarben
# untereinander bleibt bei 8.5 — dort haette 12 „nord" zerrissen.
MIN_ABSTAND_AKZENT = 11.0

SKINS = ["dark", "light", "nord", "synthwave", "ayu-hell", "melange"]


def _kanal(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hexfarbe: str) -> tuple[float, float, float]:
    h = hexfarbe.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _leuchtdichte(hexfarbe: str) -> float:
    r, g, b = _rgb(hexfarbe)
    return 0.2126 * _kanal(r) + 0.7152 * _kanal(g) + 0.0722 * _kanal(b)


def kontrast(a: str, b: str) -> float:
    la, lb = _leuchtdichte(a), _leuchtdichte(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _lab(hexfarbe: str) -> tuple[float, float, float]:
    rgb = [c / 255 for c in _rgb(hexfarbe)]
    rgb = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    r, g, b = rgb
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def abstand(a: str, b: str) -> float:
    """CIE76 — grob, aber für „sind das zwei Farben?" ausreichend."""
    la, aa, ba = _lab(a)
    lb, ab, bb = _lab(b)
    return ((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2) ** 0.5


def _skin_token(token: str) -> dict[str, str]:
    """Ein Farb-Token je Skin, direkt aus skins.css gelesen.

    Aus der CSS-Datei und nicht aus einer Kopie im Test: sonst prüfte der Test
    gegen einen Wert, den es womöglich nicht mehr gibt.
    """
    css = Path("src/moneten/static/css/skins.css").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    muster = r"(?:\[data-theme=[\"']?([\w-]+)[\"']?\][^{]*|:root[^{]*)\{([^}]*)\}"
    for m in re.finditer(muster, css):
        treffer = re.search(rf"{re.escape(token)}:\s*(#[0-9A-Fa-f]{{6}})", m.group(2))
        if treffer:
            out.setdefault(m.group(1) or "dark", treffer.group(1))
    return out


def _kartengrund() -> dict[str, str]:
    """``--bg-surface`` je Skin."""
    return _skin_token("--bg-surface")


def _farben(skin: str) -> list[tuple[str, str]]:
    pal = PALETTEN.get(skin, PALETTEN["dark"])
    sem = _SEMANTIK.get(skin, _SEMANTIK_STANDARD)
    namen = [f"chart-{i}" for i in range(len(pal))] + ["income", "save", "reserve"]
    return list(zip(namen, list(pal) + list(sem), strict=True))


@pytest.mark.parametrize("skin", SKINS)
def test_jede_diagrammfarbe_ist_gegen_die_karte_zu_sehen(skin: str) -> None:
    grund = _kartengrund()
    assert skin in grund, f"Kein --bg-surface für {skin} in skins.css"
    flaeche = grund[skin]

    zu_blass = [
        (name, farbe, round(kontrast(farbe, flaeche), 2))
        for name, farbe in _farben(skin)
        if kontrast(farbe, flaeche) < MIN_KONTRAST
    ]
    assert not zu_blass, (
        f"{skin} (Karte {flaeche}): diese Farben bleiben unter {MIN_KONTRAST}:1 "
        f"und sind als Linie kaum zu sehen — {zu_blass}"
    )


@pytest.mark.parametrize("skin", SKINS)
def test_die_diagrammfarben_bleiben_unterscheidbar(skin: str) -> None:
    """Sonst nützt der beste Kontrast nichts: zwei Linien, eine Farbe."""
    pal = list(PALETTEN.get(skin, PALETTEN["dark"]))
    zu_nah = [
        (f"chart-{i}", f"chart-{j}", round(abstand(pal[i], pal[j]), 1))
        for i in range(len(pal))
        for j in range(i + 1, len(pal))
        if abstand(pal[i], pal[j]) < MIN_ABSTAND
    ]
    assert not zu_nah, f"{skin}: diese Farbpaare sind kaum auseinanderzuhalten — {zu_nah}"


@pytest.mark.parametrize("skin", SKINS)
def test_einnahme_sparen_reserve_sind_auseinanderzuhalten(skin: str) -> None:
    """Die drei stehen im Sankey und auf der Verlaufsseite nebeneinander.

    Sie tragen eine Bedeutung, kein blosses Ordnungsmerkmal — sie zu verwechseln
    heisst, eine Einnahme für ein Defizit zu halten. Darum eine höhere Hürde als
    bei den Ordnungsfarben.
    """
    sem = list(_SEMANTIK.get(skin, _SEMANTIK_STANDARD))
    namen = ["income", "save", "reserve"]
    zu_nah = [
        (namen[i], namen[j], round(abstand(sem[i], sem[j]), 1))
        for i in range(3)
        for j in range(i + 1, 3)
        if abstand(sem[i], sem[j]) < 20
    ]
    assert not zu_nah, f"{skin}: {zu_nah}"


# ---------------------------------------------------------------------------
# Gesamtlinie gegen Konto-Linien (Vermögens-Verlauf auf der Konten-Seite)
# ---------------------------------------------------------------------------
#
# Der Fehler, den dieser Abschnitt festnagelt, war im Bild sofort sichtbar und
# in keinem Test: die Gesamtlinie zeichnet in ``--accent-primary``, und in vier
# von sechs Skins ist ``--chart-0`` DERSELBE Hex-Wert. Das erste Konto bekam
# damit exakt die Farbe der Leitlinie. Der Nutzer beschrieb es als „man weiss
# nicht, welche Linie was darstellt" — zu Recht, denn zwei davon waren
# tatsächlich nicht zu unterscheiden.
#
# Die Antwort ist ``account_charts.FARB_VERSATZ``. Dieser Test prüft nicht den
# Versatz, sondern seine Wirkung: keine gezeichnete Konto-Farbe darf der
# Akzentfarbe zu nahe kommen — egal, wie die Zuteilung später gelöst wird.


def _chart_index(css_var: str) -> int:
    """``var(--chart-3)`` → 3. Der Test misst die Farbe, die wirklich gesetzt wird."""
    treffer = re.search(r"--chart-(\d+)", css_var)
    assert treffer, f"Keine Palettenfarbe: {css_var}"
    return int(treffer.group(1))


@pytest.mark.parametrize("skin", SKINS)
def test_keine_kontolinie_traegt_die_farbe_der_gesamtlinie(skin: str) -> None:
    """Sonst behauptet das Diagramm eine Zuordnung, die es nicht gibt."""
    akzent = _skin_token("--accent-primary")[skin]
    pal = PALETTEN.get(skin, PALETTEN["dark"])

    gleich = []
    zu_nah = []
    for i in range(MAX_KONTO_LINIEN):
        farbe = pal[_chart_index(konto_farbe(i)) % len(pal)]
        if farbe.upper() == akzent.upper():
            gleich.append((i, farbe))
        elif abstand(farbe, akzent) < MIN_ABSTAND_AKZENT:
            zu_nah.append((i, farbe, round(abstand(farbe, akzent), 1)))

    assert not gleich, (
        f"{skin}: Konto-Linie(n) {gleich} tragen exakt die Akzentfarbe {akzent} "
        f"der Gesamtlinie — genau der Fehler, der die Karte unlesbar machte"
    )
    assert not zu_nah, (
        f"{skin}: diese Konto-Farben liegen der Akzentfarbe {akzent} zu nahe "
        f"(unter dE {MIN_ABSTAND_AKZENT}) — {zu_nah}"
    )


@pytest.mark.parametrize("skin", SKINS)
def test_die_gesamtlinie_ist_gegen_die_karte_zu_sehen(skin: str) -> None:
    """Die Palettenfarben sind gemessen (Test oben), ``--accent-primary`` war es
    für diesen Zweck nicht: sie ist Knopf- und Textfarbe und wurde nie als
    2.5px-Linie auf dem Kartengrund geprüft."""
    flaeche = _kartengrund()[skin]
    akzent = _skin_token("--accent-primary")[skin]
    k = kontrast(akzent, flaeche)
    assert k >= MIN_KONTRAST, (
        f"{skin}: --accent-primary ({akzent}) erreicht auf der Karte ({flaeche}) "
        f"nur {k:.2f}:1 — die Gesamtlinie des Vermögens-Verlaufs braucht "
        f"{MIN_KONTRAST}:1"
    )


# ---------------------------------------------------------------------------
# Farbwaehler der Kategorien
# ---------------------------------------------------------------------------
#
# Derselbe Farbfehler an einer zweiten Stelle. Das Kategorie-Formular stellt
# acht Farbmuster als Felder NEBENEINANDER (im Browser gemessen: 28x44 px,
# 8 px Abstand, dark, 375 px). Angeboten wurden sie aus einer eigenen Liste in
# der Vorlage — einer alten Kopie der Palette. Darin standen an Platz eins und
# zwei #D97757 und #C26851: dE 8.61, gemessen rgb(217,119,87) neben
# rgb(194,104,81). Das ist Farbe fuer Farbe dasselbe Paar mit demselben Wert,
# das oben schon als Legendenmuster durchfiel und in ``moneten.palette`` laengst
# ersetzt wurde; die Kopie hat die Korrektur nie mitbekommen. #D97757 ist im
# Skin ``dark`` zugleich --accent-primary.
#
# Die Huerde ist MIN_ABSTAND_AKZENT und nicht MIN_ABSTAND: 8.5 haette die 8.61
# durchgelassen — genau der Fehlschlag, den der Kommentar zu MIN_ABSTAND_AKZENT
# beschreibt. Hier stehen die Farben zudem nicht als Linien im Diagramm,
# sondern als Wahlmoeglichkeiten direkt nebeneinander; wer zwei davon nicht
# unterscheiden kann, kann nicht waehlen.
#
# Gegen --accent-primary wird bewusst NICHT geprueft: im Waehler ist der Akzent
# nirgends gezeichnet — die Auswahl markiert ``.cat-swatch.active`` mit
# --text-primary (gemessen: rgb(245,244,238)). Es gibt also nichts, womit sich
# ein Muster verwechseln liesse.


def _waehler_farben(client: TestClient) -> list[str]:
    """Die Hex-Farben, die das Kategorie-Formular wirklich ausliefert.

    Aus der Antwort und nicht aus der Palette gelesen: die Luecke war ja gerade,
    dass die Vorlage etwas anderes anbot als die Palette fuehrt.
    """
    html = client.get("/categories?form=new").text
    return re.findall(r'data-color="(#[0-9A-Fa-f]{6})"', html)


def test_der_farbwaehler_bietet_genau_die_palette(logged_in_client: TestClient) -> None:
    """Eine zweite Farbliste altert unbemerkt — diese hier tat es zwei Korrekturen lang."""
    assert _waehler_farben(logged_in_client) == CHART_COLORS


def test_die_farbmuster_des_waehlers_sind_auseinanderzuhalten(
    logged_in_client: TestClient,
) -> None:
    """Zwei gleich aussehende Knoepfe sind keine Wahl."""
    farben = _waehler_farben(logged_in_client)
    assert len(farben) >= 2, "Der Farbwaehler liefert keine Muster mehr"

    zu_nah = [
        (farben[i], farben[j], round(abstand(farben[i], farben[j]), 2))
        for i in range(len(farben))
        for j in range(i + 1, len(farben))
        if abstand(farben[i], farben[j]) < MIN_ABSTAND_AKZENT
    ]
    assert not zu_nah, (
        f"Farbwaehler der Kategorien: diese Muster stehen nebeneinander und sind "
        f"unter dE {MIN_ABSTAND_AKZENT} nicht zu unterscheiden — {zu_nah}"
    )


@pytest.mark.parametrize("skin", SKINS)
def test_die_sammelreihe_bleibt_unbunt_und_lesbar(skin: str) -> None:
    """„Übrige (n)" ist kein Konto und bekommt keine Palettenfarbe.

    Sie zeichnet in ``--text-tertiary``; dieser Test hält fest, dass die
    Entscheidung nicht still zu einer unsichtbaren Linie führt.
    """
    assert konto_farbe(0, rest=True) == "var(--text-tertiary)"
    flaeche = _kartengrund()[skin]
    grau = _skin_token("--text-tertiary")[skin]
    k = kontrast(grau, flaeche)
    assert k >= MIN_KONTRAST, f"{skin}: --text-tertiary erreicht nur {k:.2f}:1"


# ---------------------------------------------------------------------------
# Positions-Balken der Verlaufsseite
# ---------------------------------------------------------------------------
#
# Das gestapelte Balkendiagramm bringt drei Farbfragen mit, die es vorher nicht
# gab. Alle drei sind im Browser nachgemessen worden, und alle drei können
# still kippen, sobald jemand einen Skin ergaenzt oder ein Token verschiebt.
#
#   1. Die SCHRAFFUR der Rabatte zeichnet in --bg-surface, also in der
#      Kartenfarbe. Das ist kein Zufall: nur für diesen einen Ton ist der
#      Abstand zu JEDER Balkenfarbe gemessen (der Test ganz oben). Zeichnete sie
#      in einer eigenen Farbe, gäbe es diese Zusicherung nicht — und in einem
#      Skin verschwände das Muster, ohne dass es auffiele.
#   2. Die BEZAHLT-LINIE darf nicht aussehen wie ein Band. --accent-primary
#      schied dafür aus (in vier von sechs Skins derselbe Hexwert wie
#      --chart-0); --text-primary hält gemessen mindestens dE 26.
#   3. Der SAMMELPOSTEN „Uebrige" liegt als Band IM Stapel und muss sich von
#      jeder Palettenfarbe unterscheiden. --text-tertiary — die Wahl der
#      Sammelreihe im Vermögens-Verlauf, wo es um LINIEN geht — erreichte
#      hier nur dE 6.7 (synthwave) und 7.5 (melange) und fällt damit unter den
#      Mindestabstand der Palette. Deshalb --text-secondary.


def _token_wert(token: str, skin: str) -> str:
    """Ein Token je Skin, mit derselben Vererbung wie zur Laufzeit (Dark = Basis)."""
    werte = _skin_token(token)
    return werte.get(skin, werte["dark"])


def _var_name(css_var: str) -> str:
    """``var(--text-secondary)`` → ``--text-secondary``."""
    treffer = re.search(r"var\((--[\w-]+)\)", css_var)
    assert treffer, f"Keine CSS-Variable: {css_var}"
    return treffer.group(1)


@pytest.mark.parametrize("skin", SKINS)
def test_die_schraffur_der_rabatte_hebt_sich_von_jedem_band_ab(skin: str) -> None:
    """Sie zeichnet in der Kartenfarbe — damit gilt für sie die 3:1 von oben.

    Der Test prüft nicht die Farbe (das tut der erste Test), sondern die
    ANNAHME: dass die Schraffur wirklich ``--bg-surface`` nimmt. Griffe die
    Regel zu einem anderen Ton, wäre die Messung oben fuer sie wertlos.
    """
    css = (Path("src/moneten/static/css/theme.css")).read_text(encoding="utf-8")
    regel = re.search(r"\.vb-seg-rabatt::after\s*\{([^}]*)\}", css)
    assert regel, "Die Rabatt-Schraffur gibt es nicht mehr"
    assert "var(--bg-surface)" in regel.group(1), (
        "Die Schraffur zeichnet nicht mehr in der Kartenfarbe — damit gilt die "
        "gemessene 3:1 gegen jede Balkenfarbe nicht mehr für sie"
    )

    flaeche = _kartengrund()[skin]
    zu_blass = [
        (name, farbe, round(kontrast(flaeche, farbe), 2))
        for name, farbe in _farben(skin)
        if kontrast(flaeche, farbe) < MIN_KONTRAST
    ]
    assert not zu_blass, f"{skin}: Schraffur auf diesen Bändern kaum zu sehen — {zu_blass}"


@pytest.mark.parametrize("skin", SKINS)
def test_die_bezahlt_linie_sieht_nach_keiner_position_aus(skin: str) -> None:
    """Sonst behauptet das Diagramm eine Zuordnung, die es nicht gibt.

    Derselbe Fehler wie bei den Konto-Linien, nur eine Karte weiter: dort trug
    die erste Konto-Linie exakt die Farbe der Gesamtlinie.
    """
    css = (Path("src/moneten/static/css/theme.css")).read_text(encoding="utf-8")
    regel = re.search(r"\.vb-bezahlt\s*\{([^}]*)\}", css)
    assert regel, "Die Bezahlt-Linie gibt es nicht mehr"
    treffer = re.search(r"stroke:\s*var\((--[\w-]+)\)", regel.group(1))
    assert treffer, "Die Bezahlt-Linie zeichnet nicht über ein Token"
    linie = _token_wert(treffer.group(1), skin)

    flaeche = _kartengrund()[skin]
    k = kontrast(linie, flaeche)
    assert k >= MIN_KONTRAST, f"{skin}: Bezahlt-Linie erreicht nur {k:.2f}:1 auf der Karte"

    zu_nah = [
        (f"chart-{i}", round(abstand(linie, farbe), 1))
        for i, farbe in enumerate(PALETTEN.get(skin, PALETTEN["dark"]))
        if abstand(linie, farbe) < MIN_ABSTAND_AKZENT
    ]
    assert not zu_nah, (
        f"{skin}: die Bezahlt-Linie ({linie}) ist von diesen Bändern nicht zu "
        f"unterscheiden — {zu_nah}"
    )


@pytest.mark.parametrize("skin", SKINS)
def test_der_sammelposten_ist_von_jeder_position_zu_unterscheiden(skin: str) -> None:
    """„Uebrige" steht als Band MITTEN im Stapel, nicht daneben.

    Genau hier fiel ``--text-tertiary`` durch: in synthwave dE 6.7 zu
    ``--chart-7``, in melange 7.5 — beides unter dem Mindestabstand, den sich
    die Palette selbst gibt. Ein Sammelposten, der aussieht wie eine Position,
    macht die Legende falsch.
    """
    grau = _token_wert(_var_name(REST_FARBE), skin)
    flaeche = _kartengrund()[skin]

    k = kontrast(grau, flaeche)
    assert k >= MIN_KONTRAST, (
        f"{skin}: der Sammelposten ({grau}) erreicht auf der Karte nur {k:.2f}:1"
    )

    zu_nah = [
        (f"chart-{i}", round(abstand(grau, farbe), 1))
        for i, farbe in enumerate(PALETTEN.get(skin, PALETTEN["dark"]))
        if abstand(grau, farbe) < MIN_ABSTAND
    ]
    assert not zu_nah, (
        f"{skin}: der Sammelposten ({grau}) ist von diesen Bändern nicht zu "
        f"unterscheiden — {zu_nah}"
    )


@pytest.mark.parametrize("skin", SKINS)
def test_die_nulllinie_ist_gegen_die_karte_zu_sehen(skin: str) -> None:
    """Sie trennt „oben Kosten" von „unten Rabatt" — der Bezugspunkt des Bildes.

    In einer Rahmenfarbe gezeichnet mass sie im Standard-Skin 1.34:1 gegen
    die Karte und war nur dort zu ahnen, wo zufällig ein Balken danebenstand.
    Sie ist ein grafisches Objekt, das zum Verständnis nötig ist: 3:1 (WCAG
    1.4.11), dieselbe Hürde wie für die Bänder.

    Gelesen wird die Farbe aus theme.css und nicht aus einer Kopie im Test —
    sonst prüfte der Test einen Wert, den es womöglich nicht mehr gibt.
    """
    css = Path("src/moneten/static/css/theme.css").read_text(encoding="utf-8")
    block = re.search(r"\.vb-feld \{(.*?)\}", css, re.S)
    assert block, "Keine Regel .vb-feld in theme.css"
    treffer = re.search(r"border-bottom:[^;]*var\((--[\w-]+)\)", block.group(1))
    assert treffer, "Die Nulllinie zeichnet nicht mehr aus einem Token"

    linie = _token_wert(treffer.group(1), skin)
    flaeche = _kartengrund()[skin]
    k = kontrast(linie, flaeche)
    assert k >= MIN_KONTRAST, (
        f"{skin}: die Nulllinie ({linie}) erreicht auf der Karte nur {k:.2f}:1"
    )
