"""Der Weg, auf dem andere Leute die App bekommen — und Updates.

Drei Teile müssen zusammenpassen: der Bau-Ablauf auf GitHub, der das Abbild
erzeugt, die Compose-Datei, die es lädt, und die Ergänzung für alle, die selbst
bauen. Läuft eines davon weg, merkt es niemand hier, sondern jemand anderes bei
sich zu Hause — und der kann es nicht reparieren.

**Was hier NICHT geprüft wird:** ob der Docker-Bau gelingt. Dafür gibt es in
dieser Umgebung kein Docker; das misst der Bau-Ablauf selbst, indem er das
fertige Abbild anlaufen lässt und ``/health`` fragt, bevor er es veröffentlicht.
Dass er das tut, wird unten geprüft — ein Bau ohne Anlaufprobe beweist nur, dass
sich Dateien kopieren lassen.

Die Dateien liegen im Arbeitsordner unter ``veroeffentlichen/vorlagen/`` und im
veröffentlichten Repository in dessen Wurzel. Beide Fälle sind hier abgedeckt,
damit der Test auch dort läuft, wo er am meisten zählt: im Export.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WURZEL = Path(__file__).resolve().parents[1]
VORLAGEN = WURZEL.parent / "veroeffentlichen" / "vorlagen"

#: Im Arbeitsordner die Vorlage, im Export die Datei selbst. Der Arbeitsordner
#: hat eigene Fassungen von README und Compose — die beschreiben EINEN NAS und
#: gehören nicht hierher.
BASIS = VORLAGEN if VORLAGEN.is_dir() else WURZEL

ABBILD = BASIS / ".github" / "workflows" / "abbild.yml"
COMPOSE = BASIS / "docker-compose.yml"
QUELLBAU = BASIS / "docker-compose.quellbau.yml"
README = BASIS / "README.md"


def _dienst(pfad: Path) -> dict:
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    dienste = daten.get("services") or {}
    assert len(dienste) == 1, f"{pfad.name}: erwartet genau einen Dienst, gefunden {list(dienste)}"
    return next(iter(dienste.values()))


def test_alle_teile_sind_da() -> None:
    """Sonst prüfen die Tests unten fröhlich nichts."""
    for pfad in (ABBILD, COMPOSE, QUELLBAU, README):
        assert pfad.is_file(), f"{pfad} fehlt"


def test_die_vorgabe_laedt_ein_abbild_und_baut_nicht() -> None:
    """Wer die App nur benutzen will, soll sie nicht bauen müssen.

    Der Bau zieht Tesseract und OpenCV; auf einem kleinen NAS dauert das viele
    Minuten und braucht Platz, den es dort oft nicht gibt. Stünde hier ein
    ``build:``, würde genau das beim ersten ``up -d`` unangekündigt passieren.
    """
    dienst = _dienst(COMPOSE)
    assert "build" not in dienst, (
        "docker-compose.yml baut selbst — dann lädt niemand das fertige Abbild."
    )
    assert str(dienst.get("image", "")).startswith("ghcr.io/"), (
        f"Kein Abbild aus der Registry: {dienst.get('image')!r}"
    )


def test_der_bau_ablauf_und_die_compose_datei_meinen_dasselbe_abbild() -> None:
    """Zwei Namen, die auseinanderlaufen, ergeben ein Update, das nie ankommt."""
    aus_compose = str(_dienst(COMPOSE)["image"]).split(":")[0]
    name = aus_compose.rsplit("/", 1)[-1]
    text = ABBILD.read_text(encoding="utf-8")
    assert f"/{name}" in text, (
        f"Der Bau-Ablauf veröffentlicht nicht unter {name!r} — die Compose-Datei "
        "zeigt damit auf ein Abbild, das nie entsteht."
    )


def test_der_bau_ablauf_laesst_das_abbild_anlaufen() -> None:
    """Ein gebautes Abbild ist nicht dasselbe wie ein startendes.

    Genau daran ist der Deploy auf dem NAS mehrfach gescheitert: gebaut hat es
    immer, gestartet nicht. Hier ist die Anlaufprobe die Bedingung fürs
    Veröffentlichen.
    """
    text = ABBILD.read_text(encoding="utf-8")
    assert "/health" in text, "Der Bau-Ablauf fragt nirgends nach, ob die App antwortet"
    schritte = yaml.safe_load(text)["jobs"]["abbild"]["steps"]
    namen = [str(s.get("name", "")) for s in schritte]
    lauf = next((i for i, n in enumerate(namen) if "Anlaufen" in n), None)
    veroeff = next((i for i, n in enumerate(namen) if "Veroeffentlichen" in n), None)
    assert lauf is not None and veroeff is not None, namen
    assert lauf < veroeff, (
        "Die Anlaufprobe steht NACH dem Veröffentlichen — dann ist ein kaputtes "
        "Abbild bereits draussen, wenn sie fehlschlägt."
    )


def test_die_quellbau_ergaenzung_enthaelt_nur_den_unterschied() -> None:
    """Zwei vollständige Compose-Dateien laufen auseinander.

    Steht die Speichergrenze irgendwann nur noch in einer, merkt man am Tag des
    Fehlers, welche gegolten hat. Die Ergänzung darf deshalb ausschliesslich
    sagen, was anders ist.
    """
    dienst = _dienst(QUELLBAU)
    assert "build" in dienst, "Die Ergänzung baut gar nicht"
    zuviel = set(dienst) - {"build", "image"}
    assert not zuviel, (
        f"Die Ergänzung wiederholt Einstellungen aus docker-compose.yml: {sorted(zuviel)}. "
        "Genau so laufen zwei Dateien auseinander."
    )


def test_der_readme_erklaert_beide_wege_und_das_update() -> None:
    """Ein Weg, der nur im Kopf des Autors existiert, ist keiner."""
    text = README.read_text(encoding="utf-8")
    for satz, warum in [
        ("docker compose pull", "ohne diesen Befehl weiss niemand, wie ein Update geht"),
        ("docker-compose.quellbau.yml", "der Weg für alle, die selbst bauen, fehlt"),
        ("MONETEN_FASSUNG", "wie man auf einem Stand stehen bleibt, steht nirgends"),
        ("x86_64", "die Einschränkung der Architektur ist nicht genannt"),
    ]:
        assert satz in text, f"README: {warum} ({satz!r} kommt nicht vor)"


@pytest.mark.skipif(not (WURZEL / "src").is_dir(), reason="nur mit Quelltext daneben")
def test_die_beispielfassung_im_readme_ist_keine_erfindung() -> None:
    """Die README nennt eine Fassung als Beispiel fürs Festhalten. Sie darf
    nicht neuer sein als das, was es gibt — sonst zeigt die Anleitung auf ein
    Abbild, das niemand bauen kann."""
    import re

    from moneten import __version__

    genannt = re.findall(r"MONETEN_FASSUNG=([0-9]+\.[0-9]+\.[0-9]+)", README.read_text(encoding="utf-8"))
    assert genannt, "Kein Beispiel für MONETEN_FASSUNG im README"

    def zahlen(s: str) -> tuple[int, ...]:
        return tuple(int(t) for t in s.split("."))

    for fassung in genannt:
        assert zahlen(fassung) <= zahlen(__version__), (
            f"README nennt Fassung {fassung}, die neuste ist {__version__}"
        )


# ---------------------------------------------------------------------------
# Die Bildschirmfotos
# ---------------------------------------------------------------------------
def test_die_bilder_haben_einheitliche_groessen() -> None:
    """Zwei Handybilder nebeneinander im README müssen gleich hoch sein.

    **Gemessen, nicht vermutet.** Ein Versuch, die Höhe automatisch auf die
    nächste Kartenkante zu legen, ergab 844, 893 und 1000 Pixel — schöner
    geschnitten, aber ungleich. Nebeneinander in einer Tabelle sieht das schief
    aus, und 390×1000 ist kein Telefon mehr. Seither stehen feste Grössen im
    Aufnahme-Werkzeug; dieser Test hält sie fest.
    """
    from PIL import Image

    bilder = BASIS / "bilder"
    if not bilder.is_dir():
        pytest.skip("keine Bilder daneben")

    groessen: dict[str, set[tuple[int, int]]] = {"handy": set(), "desktop": set()}
    for p in sorted(bilder.glob("*.png")):
        art = "handy" if "handy" in p.name else "desktop" if "desktop" in p.name else None
        if art is None:
            continue
        with Image.open(p) as bild:
            groessen[art].add(bild.size)

    for art, gefunden in groessen.items():
        assert len(gefunden) <= 1, (
            f"Die {art}-Bilder haben verschiedene Grössen: {sorted(gefunden)}. "
            "Nebeneinander gestellt sieht das schief aus."
        )

    if groessen["handy"]:
        breite, hoehe = next(iter(groessen["handy"]))
        verhaeltnis = hoehe / breite
        assert 2.0 <= verhaeltnis <= 2.4, (
            f"Das Handybild ist {breite}×{hoehe} — Verhältnis {verhaeltnis:.2f}. "
            "Ein Telefon liegt bei rund 2.16 (390×844); alles darüber wirkt wie "
            "eine Textwand statt wie ein Bildschirm."
        )


def test_fremde_aktionen_haengen_an_einem_commit() -> None:
    """Ein bewegliches Tag ist kein Fixpunkt.

    Wer das Tag einer fremden Aktion umhängen kann, lässt in jedem künftigen Bau
    seinen Code mitlaufen — mit Zugriff auf Quelltext und Registry-Token. Im
    März 2025 ist genau das passiert (tj-actions/changed-files): alle
    Fassungs-Tags zeigten plötzlich auf einen bösartigen Commit.
    """
    import re

    text = ABBILD.read_text(encoding="utf-8")
    verwendet = re.findall(r"uses:\s*([^\s#]+)", text)
    beweglich = [u for u in verwendet if not re.search(r"@[0-9a-f]{40}$", u)]
    assert not beweglich, (
        f"Diese Aktionen hängen an einem beweglichen Tag statt an einem Commit: {beweglich}"
    )
