"""Tote Reste im Markup — Klassen ohne Regel, Text ohne Zweck.

Diese Tests gibt es wegen eines konkreten Fehlers: Das „Reactor"-Theme hatte
russische Zweitbeschriftungen (`<span class="ru">Обзор</span>`) in Navigation,
Kopfzeile und Fusszeile. Der Skin brachte eine eigene CSS-Datei mit, die sie
ausserhalb von Reactor ausblendete. Beim Entfernen des Themes verschwand die
CSS-Datei — das Markup blieb. Ohne die Regel stand der russische Text danach
unformatiert und sichtbar in **jedem** Theme.

Niemand hat es gemerkt: keine Ausnahme, kein Fehler im Log, alle Tests grün.
Aufgefallen ist es erst auf dem NAS, dem Nutzer.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1] / "src" / "moneten"
TEMPLATES = WURZEL / "templates"
CSS = WURZEL / "static" / "css"
JS = WURZEL / "static" / "js"

_KYRILLISCH = re.compile(r"[Ѐ-ӿ]")


def test_kein_kyrillisch_im_markup() -> None:
    """Die App ist deutschsprachig. Kyrillisch war ausschliesslich Reactor-Zierde.

    Ein präziser Test statt eines schlauen: er kostet nichts, braucht keine
    Pflege und fängt genau die Sorte Rest, die schon einmal durchgerutscht ist.
    """
    treffer = []
    for pfad in sorted(TEMPLATES.rglob("*.html")):
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if _KYRILLISCH.search(zeile):
                treffer.append(f"{pfad.name}:{nr}  {zeile.strip()[:70]}")
    assert not treffer, "Kyrillische Zeichen im Markup:\n  " + "\n  ".join(treffer)


# Klassen ohne CSS-Regel, die trotzdem berechtigt sind. Zwei Gruppen:
#
#   * **JS-Haken** — das Skript findet sie per querySelector, gestaltet wird
#     nichts. Eine leere CSS-Regel dafür wäre Ballast.
#   * **Anker im Markup** — sie benennen einen Block für Tests und für die
#     Lesbarkeit der Templates (`.kpi-card`, `.flow-card`). Auch hier ist die
#     fehlende Regel Absicht.
#
# Alles andere ist verdächtig: eine Klasse, die gestalten soll, aber keine Regel
# hat, gestaltet nichts — und ihr Inhalt steht ungestylt auf der Seite.
_ERLAUBT = {
    # JS-Haken
    "cmdk-open", "js-receipt-photo", "js-learn-toggle", "split-del",
    # Lohn-Editor: „+ Position" klont die Vorlage der eigenen Gruppe,
    # der Papierkorb entfernt seine Zeile. Beide tragen ihre Gestalt schon
    # (btn btn-ghost bzw. icon-action danger) — eine eigene Regel waere leer.
    "lohn-add", "lohn-del",
    "stress-form", "cand-select", "rcpt-chosen", "tl-card", "spark-hit",
    # Anker / Benennung
    # „nw-area" ist weg: der Vermögens-Verlauf hat keine Flächenfüllung mehr,
    # sie verdeckte den Kontrast der Konto-Linien (routers/accounts).
    # „flow-import" ist ein reiner Test-Anker am Import-Knopf der leeren
    # Geldfluss-Karte: seine Gestalt kommt von „btn", eine eigene Regel waere
    # leer. Noetig, weil 'href="/import"' auch dreimal in der Navigation steht —
    # der Nachweis darueber blieb nachgemessen gruen, als der Knopf in der Karte
    # geloescht wurde.
    "kpi-card", "vermoegen-card", "flow-card", "flow-import", "nw-card",
    "meet-card", "acc-inventory", "budget-hero", "vergeben-head",
    "bgrp-head", "kommt-liste", "cat-colorpick", "inbox-bulk-btn",
    # „abo-match" ist weg: die Händlersuche steht nicht mehr als Formular im
    # Erfassungs-Block, sondern als eigene Karte (.abo-suche) mit eigener Regel.
    "abo-stale", "rcpt-picker", "txf-more-head",
    "tx-loadmore", "quick-catmore-head",
    # Wird im Template zusammengesetzt: class="ampel-{{ ... }}"
    "ampel-",
}


def _klassen_im_markup() -> dict[str, list[str]]:
    verwendet: dict[str, list[str]] = defaultdict(list)
    for pfad in sorted(TEMPLATES.rglob("*.html")):
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            for attribut in re.findall(r'class="([^"]*)"', zeile):
                # Jinja-Ausdrücke entfernen, sonst landen {% if %}-Teile in der Liste.
                sauber = re.sub(r"\{[{%].*?[%}]\}", " ", attribut)
                for k in sauber.split():
                    if k and not k.startswith("{"):
                        verwendet[k].append(f"{pfad.name}:{nr}")
    return verwendet


def test_keine_klasse_ohne_regel() -> None:
    """Eine Klasse ohne CSS-Regel gestaltet nichts — ihr Inhalt steht nackt da.

    Fand beim Schreiben sofort zwei echte Fälle: die Reactor-Reste `.ru` /
    `.ru-eyebrow` / `.ru-block`, und ein `.mb-2` in `prices.html`, das es als
    Utility gar nicht gab (die Artikelzeilen klebten aneinander).
    """
    css = "\n".join(p.read_text(encoding="utf-8") for p in CSS.rglob("*.css"))
    definiert = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))

    fehlend = {
        k: orte for k, orte in _klassen_im_markup().items()
        if k not in definiert and k not in _ERLAUBT
    }
    assert not fehlend, (
        "Diese Klassen stehen im Markup, haben aber keine CSS-Regel:\n  "
        + "\n  ".join(
            f".{k}  ({len(o)}x, z.B. {o[0]})" for k, o in sorted(fehlend.items())
        )
        + "\n\nIst das Absicht (JS-Haken oder blosser Anker), gehört sie in "
          "_ERLAUBT in dieser Datei — mit Begründung."
    )


def test_erlaubnisliste_ist_nicht_verwahrlost() -> None:
    """Einträge, die es im Markup gar nicht mehr gibt, gehören raus.

    Sonst wächst die Liste still weiter und deckt irgendwann echte Fehler zu.
    """
    verwendet = set(_klassen_im_markup())
    tot = sorted(k for k in _ERLAUBT if k not in verwendet)
    assert not tot, (
        f"Diese Einträge in _ERLAUBT kommen im Markup nicht mehr vor: {tot}"
    )


# ---------------------------------------------------------------------------
# Gegenrichtung: CSS-Regel bleibt, Markup verschwindet
# ---------------------------------------------------------------------------
#
# Der Reactor-Fall lief genau andersherum als der Test darüber: die CSS-Datei
# ging, das Markup blieb. Beim RÜCKBAU passiert regelmässig das Gegenteil — ein
# Block wird aus dem Template geworfen, seine Regeln bleiben stehen. Das tut
# niemandem weh und ist deshalb doppelt zäh: die Datei wächst, und beim nächsten
# Umbau rät man, was noch gebraucht wird.

# Klassen, für die es eine Regel gibt, aber keine Fundstelle im Code.
# Zwei Gruppen, beide begründet.
#
# Eine dritte gab es einmal: „Altbestand, aufgenommen beim Einführen dieses
# Tests" — rund 40 Rückbau-Reste, geparkt mit dem Vermerk, die Liste sei eine
# Schuld und kein Freibrief. Diese Schuld ist  abgetragen: die
# Regeln sind aus theme.css entfernt, die Einträge hier gestrichen. Was jetzt
# noch steht, steht aus einem Grund — nicht aus Gewohnheit.
_CSS_ERLAUBT = {
    # 1. Wird nie ausgeschrieben, sondern im Template zusammengesetzt — ein
    #    Namens-Suchlauf kann sie gar nicht finden. Die Werte liefert
    #    `ampel_status()` (services/median_budget.py), eingesetzt wird über
    #    `class="ampel-{{ r.ampel }}"` in budget_root.html und savings_root.html.
    #    Der vierte Wert „ok" fehlt hier, weil savings_root.html ihn an einer
    #    Stelle ausschreibt und der Suchlauf ihn dort findet.
    "ampel-none", "ampel-warn", "ampel-over",

    # 2. Utility-Vorrat — und anders als der Altbestand oben KEINE Schuld: ein
    #    Utility-Satz darf vollständig sein, sonst muss beim nächsten Layout die
    #    fehlende Hälfte neu erfunden werden. `grid-3`/`grid-4` gehören zur
    #    lückenlosen Reihe grid-2/3/4 (grid-2 ist in Gebrauch) und stehen mit in
    #    den @media-Umbruchregeln; `justify-center` vervollständigt die
    #    Flex-Utilities neben justify-between; `text-base`/`text-lg` die
    #    Schriftgrössen-Reihe neben dem benutzten `text-sm`. `sr-only` ist ein
    #    Barrierefreiheits-Werkzeug (Text nur für Screenreader) — das hält man
    #    vor, statt es beim ersten Bedarf falsch nachzubauen.
    "grid-3", "grid-4", "justify-center", "sr-only", "text-base", "text-lg",
}

# Kommentare weg (dort steht Prosa wie „die .card-Regel"), und der Punkt darf
# nicht Teil eines Dateinamens („skins.css") oder einer Zahl (".5rem") sein.
_KLASSE_IN_CSS = re.compile(r"(?<![\w\"'/.-])\.([a-zA-Z][\w-]*)")


def _klassen_in_css() -> dict[str, str]:
    """Alle Klassen mit CSS-Regel → erste Fundstelle."""
    gefunden: dict[str, str] = {}
    for pfad in sorted(CSS.rglob("*.css")):
        ohne_kommentar = re.sub(r"/\*.*?\*/", " ", pfad.read_text(encoding="utf-8"), flags=re.S)
        for nr, zeile in enumerate(ohne_kommentar.splitlines(), 1):
            for k in _KLASSE_IN_CSS.findall(zeile):
                gefunden.setdefault(k, f"{pfad.name}:{nr}")
    return gefunden


def _namen_im_code() -> set[str]:
    """Jeder Bezeichner, der irgendwo in Templates, JS oder Python vorkommt.

    Bewusst grosszügig (nicht nur ``class="…"``): eine Klasse reist auch als
    Makro-Parameter (``btn_cls="cat-picker-btn"``), aus einer Python-Funktion
    (``icons.py``) oder aus einem ``classList.add`` ins Markup. Für die Frage
    „gibt es diese Regel für irgendetwas?" ist jede Nennung ein Beleg — für die
    Gegenrichtung (Klasse ohne Regel) bleibt der strengere Test oben zuständig.
    """
    namen: set[str] = set()
    for pfad in [*TEMPLATES.rglob("*.html"), *JS.rglob("*.js"), *WURZEL.rglob("*.py")]:
        namen |= set(re.findall(r"[A-Za-z][\w-]*", pfad.read_text(encoding="utf-8")))
    return namen


def _verwaiste(css: dict[str, str], namen: set[str], erlaubt: set[str]) -> dict[str, str]:
    """CSS-Klassen, die im Code nirgends vorkommen (ohne die erlaubten)."""
    return {k: ort for k, ort in css.items() if k not in namen and k not in erlaubt}


def test_verwaiste_regel_wird_erkannt() -> None:
    """Prüft die Prüfung — sonst könnte sie stillschweigend nichts mehr finden.

    Nachgestellt wird der echte Fall in seiner Rückbau-Richtung: die Regel
    ``.ru-eyebrow`` steht noch in der CSS, das Markup dazu ist weg. Wäre die
    Erkennung kaputt (falsches Muster, leere Namensmenge), bliebe der Test
    darunter grün, ohne noch irgendetwas zu prüfen.
    """
    css = {"ru-eyebrow": "theme.css:12", "card": "theme.css:34"}
    assert _verwaiste(css, {"card"}, set()) == {"ru-eyebrow": "theme.css:12"}
    assert _verwaiste(css, {"card"}, {"ru-eyebrow"}) == {}


def test_keine_css_regel_ohne_markup() -> None:
    """Eine Regel, die kein Markup mehr trifft, ist toter Ballast.

    Sie kostet Bytes bei jedem Seitenaufruf und — teurer — Zeit beim nächsten
    Umbau: wer eine Klasse in der CSS findet, nimmt an, dass sie gebraucht wird.
    """
    verwaist = _verwaiste(_klassen_in_css(), _namen_im_code(), _CSS_ERLAUBT)
    assert not verwaist, (
        "Diese CSS-Regeln treffen kein Markup mehr:\n  "
        + "\n  ".join(f".{k}  ({ort})" for k, ort in sorted(verwaist.items()))
        + "\n\nEntweder die Regel entfernen oder — wenn der Name zusammengesetzt "
          "bzw. nur von JS gesetzt wird — mit Begründung in _CSS_ERLAUBT "
          "aufnehmen."
    )


def test_css_erlaubnisliste_ist_nicht_verwahrlost() -> None:
    """Einträge, für die es gar keine Regel mehr gibt, gehören raus.

    Vor allem die Altbestands-Gruppe: wird eine tote Regel entfernt, muss ihr
    Eintrag mitgehen. Sonst schrumpft die Schuld nur scheinbar, und die Liste
    deckt beim nächsten Mal einen echten Fall zu.
    """
    css = set(_klassen_in_css())
    tot = sorted(k for k in _CSS_ERLAUBT if k not in css)
    assert not tot, f"Diese Einträge in _CSS_ERLAUBT haben keine CSS-Regel mehr: {tot}"


def test_kein_verweis_auf_das_entfernte_theme() -> None:
    """Reactor ist entfernt — Markup und Skript dürfen es nicht mehr erwähnen.

    Kommentare in CSS/Python sind ausgenommen: dort ist die Erwähnung
    Projektgeschichte („der frühere Reactor-Skin wurde entfernt"), und die zu
    tilgen würde die Begründung mitlöschen.
    """
    treffer = []
    for pfad in [*sorted(TEMPLATES.rglob("*.html")), *sorted(JS.rglob("*.js"))]:
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"reactor|reaktor", zeile, re.IGNORECASE):
                treffer.append(f"{pfad.name}:{nr}  {zeile.strip()[:70]}")
    assert not treffer, "Reactor-Erwähnungen in Markup/JS:\n  " + "\n  ".join(treffer)


def test_kein_stylesheet_ausserhalb_der_geladenen() -> None:
    """Liegt CSS herum, das die App gar nicht laedt?

    ZWEIMAL passiert, beide Male mit demselben Ablauf: mehrere Agenten arbeiten
    parallel und legen ihre Regeln in ``static/css/parts/`` ab, damit sie sich
    nicht gegenseitig ueberschreiben. Danach muss jemand sie in ``theme.css``
    zusammenfuehren. Wurde das vergessen, lieferte die App **unformatiertes
    Markup** aus — die Steuerseite zeigte nackte Zahlen, die Sparziele-Seite
    riesige schwarze Kreise (ein SVG ohne Groesse, dessen Kreis mangels
    ``fill: none`` schwarz fuellt).

    Der Test darauf gab es nicht: ``test_keine_klasse_ohne_regel`` durchsucht
    nur die GELADENEN Stylesheets. Eine Regel in ``parts/`` zaehlt dort nicht
    als vorhanden — die Klasse gilt als verwaist, und trotzdem lief die Suite
    gruen, weil die Datei ja existierte.

    Ab jetzt ist die Suite ROT, solange etwas Ungeladenes herumliegt. Damit
    kann kein Deploy mehr freigegeben werden, dessen Oberflaeche ungestaltet
    waere.
    """
    css_dir = Path("src/moneten/static/css")
    # Welche Stylesheets die Seite wirklich einbindet — aus base.html gelesen
    # und nicht hier aufgezaehlt: eine Kopie im Test liefe irgendwann mit dem
    # Original auseinander und pruefte gegen einen Stand, den es nicht gibt.
    basis = Path("src/moneten/templates/base.html").read_text(encoding="utf-8")
    geladen = set(re.findall(r"css/([\w.-]+\.css)", basis))
    assert geladen, "In base.html ist kein einziges Stylesheet verlinkt"

    fremd = sorted(
        str(f.relative_to(css_dir))
        for f in css_dir.rglob("*.css")
        if f.name not in geladen
    )
    assert not fremd, (
        "Diese Stylesheets laedt die App nicht — ihre Regeln wirken nirgends. "
        f"Zusammenfuehren und entfernen: {fremd}"
    )


def test_jedes_verlinkte_stylesheet_existiert() -> None:
    """Zeigt ein <link> auf eine Datei, die es nicht gibt?

    Die GEGENRICHTUNG zu ``test_kein_stylesheet_ausserhalb_der_geladenen`` — und
    genau die fehlte, als es darauf ankam: beim Zusammenfuehren der Teil-
    Stylesheets wurde ``static/css/parts/`` geloescht, die vier ``<link>``-Zeilen
    in ``base.html`` blieben stehen. Jeder Seitenaufruf erzeugte vier 404er, der
    Browser meldete „Refused to apply style … MIME type application/json", und
    die Suite blieb gruen — der andere Test prueft ja nur, dass keine Datei
    herumliegt, nicht dass jede verlinkte da ist.

    Zwei Tests, weil es zwei verschiedene Fehler sind: Datei ohne Verweis (wirkt
    nicht) und Verweis ohne Datei (404 bei jedem Aufruf).
    """
    basis = Path("src/moneten/templates/base.html").read_text(encoding="utf-8")
    statisch = Path("src/moneten/static")

    fehlend = [
        pfad
        for pfad in re.findall(r'href="/static/([\w./-]+\.css)"', basis)
        if not (statisch / pfad).is_file()
    ]
    assert not fehlend, (
        "base.html verweist auf Stylesheets, die es nicht gibt — jeder "
        f"Seitenaufruf holt sich dafuer einen 404: {fehlend}"
    )


def test_statische_dateien_haengen_am_inhalt_nicht_an_der_version() -> None:
    """Aendert sich der Cache-Schluessel, wenn sich das CSS aendert?

    DER FEHLER, DEN DAS VERHINDERT — er hat Stunden gekostet und war in keinem
    Testlauf sichtbar: die Links trugen ``?v={{ app_version }}``. Wird die
    Version bei einem Deploy nicht erhoeht (Normalfall bei Zwischenfassungen),
    bleibt der Schluessel gleich und der Browser behaelt sein altes Stylesheet.
    Der Nutzer bekam neue Vorlagen mit altem CSS: SVG-Kreise in schwarzer
    Standardfuellung, Legenden als nackte Wortketten, fehlende Warnstreifen.
    Die Dateien im Repo waren dabei die ganze Zeit richtig — kein Test konnte
    es zeigen, weil kein Test den Auslieferungsweg prueft.

    Zwei Zusicherungen: der Schluessel steht ueberhaupt an jedem Verweis, und er
    haengt am INHALT. Der zweite Teil ist der wichtige — ``app_version`` wuerde
    den ersten auch erfuellen.
    """
    basis = Path("src/moneten/templates/base.html").read_text(encoding="utf-8")

    ohne_schluessel = [
        treffer
        for treffer in re.findall(r'href="(/static/[\w./-]+\.(?:css|js))"', basis)
    ]
    assert not ohne_schluessel, (
        f"Diese statischen Dateien haben keinen Cache-Schluessel: {ohne_schluessel}"
    )

    # Und zwar den INHALTS-Schluessel. Ohne diese Zeile ueberlebt die Mutation
    # „zurueck auf app_version" den Test — genau der Zustand, der den Fehler
    # verursacht hat.
    falsch = re.findall(r'href="/static/[\w./-]+\.(?:css|js)\?v=\{\{\s*(\w+)', basis)
    assert falsch and set(falsch) == {"statik_v"}, (
        f"Statische Dateien haengen am falschen Schluessel: {sorted(set(falsch))}. "
        "app_version aendert sich bei Zwischenfassungen NICHT — der Browser "
        "behaelt dann sein altes Stylesheet."
    )

    from moneten.templating import _statik_fingerabdruck

    vorher = _statik_fingerabdruck()
    css = Path("src/moneten/static/css/theme.css")
    inhalt = css.read_bytes()
    try:
        css.write_bytes(inhalt + b"/* Probe */")
        nachher = _statik_fingerabdruck()
    finally:
        css.write_bytes(inhalt)

    assert nachher != vorher, (
        "Der Cache-Schluessel bleibt gleich, obwohl sich theme.css geaendert hat "
        "— der Browser wuerde weiter das alte Stylesheet ausliefern"
    )
    assert _statik_fingerabdruck() == vorher, "Wiederherstellung fehlgeschlagen"
