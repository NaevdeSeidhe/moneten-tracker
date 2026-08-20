"""Das Dock — alles, was am unteren Fensterrand FEST steht.

Am Handy stapeln sich dort bis zu vier Dinge: die Tab-Leiste ganz unten, auf
Formularseiten ein seitenweiter Aktionsbalken, darüber die Schwebeknöpfe, und
ganz oben der Toast. Früher trug jede Regel ihre eigene px-Zahl (76, 74, 84,
78, 138). Zwei davon zeigten auf dieselbe Reihe: der Buchen-Knopf auf /quick
lag unter den Schwebeknöpfen, 122 von 349 Knopf-Pixeln waren tot — der
Trefferpunkt lieferte den Knopf, nicht den Balken.

Seither rechnet ``theme.css`` alles aus einer Quelle: drei Höhen
(``--dock-nav-h``, ``--dock-bar-h``, ``--dock-fab-h``) plus ``--dock-gap``,
daraus die Unterkante jeder Reihe und ``--dock-h`` für den reservierten
Streifen. ``initDock()`` in ``app.js`` misst die Höhen am echten Kasten nach.

Diese Datei gab es lange nicht — und ohne sie liess sich das ganze System
lautlos zerlegen. Nachgemessen blieben alle 661 Tests grün, wenn man
``--dock-h`` an der Shell auf 0 setzte, die Reihe des Aktionsbalkens löschte,
das Zeilenmenü wieder unter die Knöpfe legte oder ``initDock`` aus ``boot()``
aushängte. Jeder Test hier hält genau eine dieser Stellen.

Geprüft wird ohne Browser: ``theme.css`` und ``app.js`` werden geparst. Das
fängt nicht jede Überlagerung, aber jede, die schon einmal passiert ist.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "moneten" / "static"
THEME = STATIC / "css" / "theme.css"
APP_JS = STATIC / "js" / "app.js"

# Bis hierher gibt es die Tab-Leiste und damit überhaupt ein Dock (siehe
# .mobile-nav in theme.css). Breiter steht unten nichts Festes, dort sind alle
# Dock-Höhen bewusst 0.
DOCK_BREAKPOINT = 820


def _ohne_kommentare(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _bloecke(css: str) -> list[tuple[str, str]]:
    """``[(media_bedingung, rumpf)]`` — was ausserhalb jeder Query steht, hat ``""``.

    Ein Mini-Scanner statt einer CSS-Bibliothek: die Datei verschachtelt nur
    eine Ebene (``@media { … }``), und Klammern zählen kostet keine
    Abhängigkeit.
    """
    treffer: list[tuple[str, str]] = []
    rest: list[str] = []
    i = 0
    while True:
        m = re.compile(r"@media([^{]*)\{").search(css, i)
        if not m:
            rest.append(css[i:])
            break
        rest.append(css[i:m.start()])
        tiefe, j = 1, m.end()
        while j < len(css) and tiefe:
            if css[j] == "{":
                tiefe += 1
            elif css[j] == "}":
                tiefe -= 1
            j += 1
        treffer.append((m.group(1).strip(), css[m.end():j - 1]))
        i = j
    treffer.append(("", "".join(rest)))
    return treffer


def _regeln() -> list[tuple[str, str, dict[str, str]]]:
    """``[(media_bedingung, selektor, {eigenschaft: wert})]`` für die ganze Datei."""
    aus: list[tuple[str, str, dict[str, str]]] = []
    for media, rumpf in _bloecke(_ohne_kommentare(THEME.read_text(encoding="utf-8"))):
        for selektor, block in re.findall(r"([^{}]+)\{([^{}]*)\}", rumpf):
            deklarationen: dict[str, str] = {}
            for zeile in block.split(";"):
                if ":" not in zeile:
                    continue
                name, _, wert = zeile.partition(":")
                deklarationen[name.strip()] = wert.strip()
            aus.append((media, " ".join(selektor.split()), deklarationen))
    return aus


def _im_dock_kontext(media: str) -> bool:
    """Gilt die Query dort, wo die Tab-Leiste steht (≤820px)?"""
    breiten = [int(n) for n in re.findall(r"max-width:\s*(\d+)px", media)]
    return bool(breiten) and min(breiten) <= DOCK_BREAKPOINT


def _wert(klasse: str, eigenschaft: str, *, dock: bool | None = None) -> list[str]:
    """Alle Werte, die eine Regel für diese Klasse setzt (Reihenfolge der Datei).

    ``dock=True`` beschränkt auf Regeln, die im Dock-Kontext gelten,
    ``dock=False`` auf alle übrigen.
    """
    # Als ganzes Klassen-Wort, sonst zählte .fab-search als .fab mit.
    muster = re.compile(r"(?<![\w-])" + re.escape(klasse) + r"(?![\w-])")
    aus = []
    for media, selektor, deklarationen in _regeln():
        if eigenschaft not in deklarationen or not muster.search(selektor):
            continue
        if dock is not None and _im_dock_kontext(media) is not dock:
            continue
        aus.append(deklarationen[eigenschaft])
    return aus


def _js_funktion(name: str) -> str:
    """Rumpf einer Funktion aus app.js (alles bis zur schliessenden Klammer)."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index(f"function {name}(")
    return js[start:js.index("\n  }", start)]


# ---------------------------------------------------------------------------
# Die Reihen: jede bezieht ihre Unterkante aus dem Dock, keine eigene px-Zahl
# ---------------------------------------------------------------------------

# Element -> Token, aus dem seine Unterkante kommen MUSS. Von unten nach oben:
# Tab-Leiste (Grund), Aktionsbalken, Schwebeknöpfe, Toast.
REIHEN = {
    ".quick-submit": "--dock-bar-bottom",
    ".fab": "--dock-fab-bottom",
    ".toast-host": "--dock-toast-bottom",
}


def test_jede_schwebende_reihe_ankert_an_einem_dock_token() -> None:
    """Der Kern des Vertrags — und genau die Stelle, an der es schon brach.

    Eine eigene Zahl an einem dieser Elemente ist nicht ungefähr richtig,
    sondern eine zweite Wahrheit: sie weiss nichts davon, ob die Reihe unter
    ihr belegt ist. So kam der Buchen-Knopf auf die Höhe der Schwebeknöpfe.
    """
    for klasse, token in REIHEN.items():
        werte = _wert(klasse, "bottom", dock=True)
        assert werte, (
            f"{klasse} setzt im Dock-Kontext (max-width {DOCK_BREAKPOINT}px) gar "
            f"kein bottom — dann gilt der Desktop-Wert, und dort steht unten nichts."
        )
        for wert in werte:
            assert wert == f"var({token})", (
                f"{klasse} steht auf {wert!r} statt auf var({token}). Eine nackte "
                f"Zahl kennt die Reihen unter sich nicht."
            )


def test_die_tab_leiste_ist_der_boden_des_stapels() -> None:
    """Sie ist die einzige Reihe mit einer eigenen Zahl — und die muss 0 sein.

    Läge sie höher, entstünde unter ihr ein Streifen, den keine Rechnung kennt.
    """
    werte = _wert(".mobile-nav", "bottom", dock=True)
    assert werte, ".mobile-nav setzt kein bottom"
    assert all(w == "0" for w in werte), f".mobile-nav klebt nicht am Rand: {werte}"


def test_die_reihen_stapeln_sich_lueckenlos_aufeinander() -> None:
    """Jede Unterkante = Unterkante der Reihe darunter + deren Höhe.

    Ohne diese Kette wäre jedes Token eine eigene Zahl, und eine belegte Reihe
    (etwa der Aktionsbalken auf Formularseiten) würde von der nächsten
    schlicht übersprungen.
    """
    wurzel = next(d for m, s, d in _regeln() if m == "" and s == ":root")
    kette = {
        "--dock-bar-bottom": ("--dock-nav-h", "--dock-gap"),
        "--dock-fab-bottom": ("--dock-bar-bottom", "--dock-bar-h"),
        "--dock-toast-bottom": ("--dock-fab-bottom", "--dock-fab-h"),
    }
    for token, summanden in kette.items():
        assert token in wurzel, f"{token} fehlt in :root"
        for summand in summanden:
            assert f"var({summand})" in wurzel[token], (
                f"{token} rechnet ohne {summand} — die Reihe darunter wird "
                f"übersprungen."
            )


def test_der_seiteninhalt_reserviert_das_ganze_band() -> None:
    """Sonst endet die Seite unter den Knöpfen.

    Der Abstand gehört an die ``.app-shell`` (die hat ``min-height: 100vh``),
    nicht an den Footer: auf einer Seite, die kürzer als das Fenster ist,
    schiebt zusätzlicher Fussabstand kein einziges Bedienelement nach oben.
    """
    werte = _wert(".app-shell", "padding-bottom", dock=False)
    assert werte, ".app-shell reserviert unten gar nichts"
    assert all("var(--dock-h)" in w for w in werte), (
        f".app-shell reserviert {werte!r} statt var(--dock-h) — das Band wird "
        f"aus vier Grössen gerechnet, eine feste Zahl läuft davon weg."
    )
    dock_h = [
        d["--dock-h"] for m, s, d in _regeln()
        if _im_dock_kontext(m) and s == ":root" and "--dock-h" in d
    ]
    assert dock_h, (
        f"--dock-h wird bis {DOCK_BREAKPOINT}px nie gefüllt und bleibt 0 — die "
        f"Seite endet unter der Tab-Leiste."
    )
    for wert in dock_h:
        assert "var(--dock-fab-bottom)" in wert and "var(--dock-fab-h)" in wert, (
            f"--dock-h = {wert!r} reicht nicht bis zur Oberkante der obersten "
            f"belegten Reihe."
        )


def test_seiten_mit_aktionsbalken_belegen_eine_eigene_reihe() -> None:
    """Ohne diese Regel liegen Balken und Schwebeknöpfe wieder aufeinander.

    ``--dock-fab-bottom`` rechnet ``--dock-bar-h`` mit ein. Bleibt die Höhe 0,
    obwohl ein ``.quick-submit`` auf der Seite steht, ankern beide auf
    derselben Unterkante — der gemessene Ursprungsfehler.
    """
    treffer = [
        d["--dock-bar-h"] for m, s, d in _regeln()
        if _im_dock_kontext(m) and ":has(.quick-submit)" in s and "--dock-bar-h" in d
    ]
    assert treffer, (
        "Keine Regel gibt Seiten mit .quick-submit eine eigene Balken-Reihe "
        "(:root:has(.quick-submit) { --dock-bar-h: … }). Die Schwebeknöpfe "
        "landen dann auf dem Balken."
    )
    for wert in treffer:
        zahlen = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)px", wert)]
        assert zahlen and max(zahlen) > 0, (
            f"--dock-bar-h ist {wert!r} — eine Reihe der Höhe 0 verschiebt nichts."
        )


# ---------------------------------------------------------------------------
# Stapelung in der Tiefe: was der Nutzer eben geöffnet hat, gehört nach vorn
# ---------------------------------------------------------------------------

def _z_index(klasse: str) -> int:
    werte = _wert(klasse, "z-index")
    assert werte, f"{klasse} hat keinen z-index"
    return max(int(w) for w in werte)


def test_das_zeilenmenue_liegt_ueber_dem_dock() -> None:
    """Es klappt nach UNTEN auf und landet damit im Streifen der Knöpfe.

    Gemessen bei 375px, Zeile auf y600: der Löschen-Eintrag (x172–324,
    y685–729) lag zu 73 seiner 152 Pixel unter den Schwebeknöpfen, der
    Trefferpunkt lieferte den Knopf. Ein Menü, das der Nutzer gerade erst
    geöffnet hat, gehört über die dauerhaft schwebende Abkürzung — aber unter
    das Mehr-Blatt, das den Bildschirm bewusst übernimmt.
    """
    menue = _z_index(".rowmenu-pop")
    for klasse in (".fab", ".mobile-nav", ".quick-submit"):
        assert menue > _z_index(klasse), (
            f".rowmenu-pop (z {menue}) liegt unter {klasse} "
            f"(z {_z_index(klasse)}) — das aufgeklappte Menü ist dort nicht "
            f"bedienbar, der Tap trifft das Element darüber."
        )
    assert menue < _z_index(".more-sheet"), (
        f".rowmenu-pop (z {menue}) überdeckt das Mehr-Blatt "
        f"(z {_z_index('.more-sheet')})."
    )


# ---------------------------------------------------------------------------
# Die Nachmessung im Browser
# ---------------------------------------------------------------------------

def test_boot_startet_die_dock_messung() -> None:
    """Ohne den Aufruf bleiben die Höhen auf den Werten des Blattes stehen.

    Die sind für die Standard-Schriftgrösse richtig — stellt der Nutzer im
    Browser eine grössere ein, wächst die Tab-Leiste, und der reservierte
    Streifen wüchse nicht mit.
    """
    assert "initDock()" in _js_funktion("boot"), (
        "boot() ruft initDock() nicht auf — die Dock-Höhen werden nie nachgemessen."
    )


def test_gemessene_null_ersetzt_die_statische_ruecklage_nicht() -> None:
    """Reproduzierter Fehler: die Messung konnte das ganze Band kollabieren lassen.

    ``messen()`` schrieb die drei Höhen als INLINE-Style auf ``:root``. Inline
    schlägt jede Media-Query — auch die Rückfallebene in ``theme.css``. Am PC
    misst die Funktion korrekt drei Nullen (unten steht dort nichts fest). Gilt
    danach die Query bis 820px, ohne dass neu gemessen wurde, stehen diese
    Nullen weiter im ``style``-Attribut und schlagen die 66px/64px aus dem
    Blatt: gemessen fiel ``--dock-h`` auf 10px, Tab-Leiste (y958–1024),
    Schwebeknöpfe (y960–1014) und Aktionsbalken (y951–1014) lagen übereinander.
    Reproduziert durch Verkleinern von 1280 auf 768 ohne resize-Ereignis.

    Eine gemessene 0 heisst deshalb nicht "0px schreiben", sondern "hier ist
    nichts zu messen": die Eigenschaft muss vom Element verschwinden, damit
    wieder das Blatt gilt.
    """
    quelle = _js_funktion("initDock")

    direkt = re.findall(r'setProperty\(\s*"(--dock-[a-z-]+)"', quelle)
    assert not direkt, (
        f"initDock schreibt {direkt} unbedingt als Inline-Style. Eine gemessene "
        f"0 löscht damit die statische Rücklage aus theme.css."
    )
    assert "removeProperty" in quelle, (
        "initDock entfernt nie eine Dock-Höhe — dann bleibt eine gemessene 0 "
        "als Inline-Style stehen und schlägt die Media-Query."
    )
    assert re.search(r"\bwert\s*>\s*0\b", quelle), (
        "Es fehlt die Unterscheidung zwischen gemessen-und-belegt und "
        "nichts-zu-messen; ohne sie wird auch die 0 geschrieben."
    )
