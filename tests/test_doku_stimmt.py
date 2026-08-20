"""Die Dokumentation muss beschreiben, was der Code tut.

Falsche Doku ist in diesem Projekt kein Schönheitsfehler, sondern ein
wiederkehrender Fehler mit Geschichte: die Testanzahl stand irgendwann bei 163,
während 322 liefen; das Übergabe-Dokument führte längst Erledigtes als offen;
zwei fertige Seiten fehlten in der API-Referenz. Jedes Mal war die Ursache
dieselbe — eine Zahl oder Liste, die von Hand nachgeführt werden musste.

Diese Tests führen sie nicht nach, sie melden nur, dass es nötig ist. Sie sind
absichtlich stur: lieber ein Testlauf, der zum Aktualisieren zwingt, als eine
Doku, der niemand mehr traut.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
DOCS = WURZEL / "docs"

# Die ausfuehrliche Doku ist eine INTERNE Notizsammlung und wird nicht
# mitgeliefert. Wo sie fehlt, gibt es nichts abzugleichen — und ein Test, der
# eine fehlende Datei als Fehler meldet, sagt nur, dass sie fehlt. Das weiss man
# schon. Er wuerde aber jeden Testlauf einer frischen Installation rot faerben,
# und ein rotes Ergebnis, das niemanden etwas angeht, bringt alle anderen um
# ihre Wirkung.
#
# Der Schnitt ist bewusst NICHT modulweit: der letzte Test dieser Datei liest
# ausschliesslich ``pyproject.toml``, und die liegt in jedem Klon. Modulweit
# übersprungen verlor der Export einen Wächter, der dort voll anwendbar ist.
_OHNE_DOCS = pytest.mark.skipif(
    not DOCS.is_dir(), reason="docs/ nicht vorhanden — nichts abzugleichen"
)

# Routen, die bewusst nicht in der Referenz stehen.
_OHNE_DOKU = {
    "/static",       # Dateiauslieferung, kein Endpunkt der App-Logik
    "/openapi.json", # abgeschaltet
    "/docs",
    "/redoc",
}


def _registrierte_pfade() -> set[str]:
    """Alle GET/POST-Pfade der laufenden App — ohne Pfadparameter-Details."""
    from moneten.main import app

    pfade = set()
    for route in app.routes:
        pfad = getattr(route, "path", None)
        if not pfad or any(pfad.startswith(x) for x in _OHNE_DOKU):
            continue
        # /transactions/{tx_id}/edit → /transactions — dokumentiert wird der Bereich.
        pfade.add("/" + pfad.lstrip("/").split("/")[0].split("{")[0])
    return {p for p in pfade if p != "/"}


@_OHNE_DOCS
def test_jede_seite_steht_in_der_api_referenz() -> None:
    """Eine neue Seite ohne Eintrag in der Referenz fällt sonst niemandem auf.

    Genau so fehlten `/steuern` und `/preise`: beide fertig, beide verlinkt,
    beide in keiner Doku.
    """
    text = (DOCS / "04_API_REFERENZ.md").read_text(encoding="utf-8")
    fehlend = sorted(p for p in _registrierte_pfade() if f"`{p}" not in text)
    assert not fehlend, (
        "Diese Routen sind registriert, stehen aber nicht in docs/04_API_REFERENZ.md:\n  "
        + "\n  ".join(fehlend)
    )


@_OHNE_DOCS
def test_dokumentierte_testanzahl_stimmt(request) -> None:
    """Jede Testzahl in README und Architektur-Doku muss die tatsächliche sein.

    Gezählt wird, was pytest gerade gesammelt hat — die Zahl kann also nicht
    veralten, ohne dass dieser Test es meldet. Bei einem eingeschränkten Lauf
    (einzelne Datei, ``-k``) wäre der Vergleich sinnlos, dann wird übersprungen.
    """
    import pytest

    config = request.config
    ziele = [a for a in config.args if not a.startswith("-")]
    # Ein EINZELNES Ziel genügte nicht: ``pytest tests/test_doku_stimmt.py`` ist
    # genau ein Ziel und sammelte vier Tests — der Vergleich mit der
    # dokumentierten Gesamtzahl schlug dann fehl, obwohl nichts falsch war.
    # Aussagekräftig ist der Lauf nur, wenn jedes Ziel den ganzen Testbaum meint.
    ganzer_baum = all(
        Path(z.split("::")[0]).resolve() in (WURZEL.resolve(), (WURZEL / "tests").resolve())
        for z in ziele
    )
    if not ganzer_baum or config.option.keyword or config.option.markexpr:
        pytest.skip("Nur bei einem vollständigen Lauf aussagekräftig")

    ist = request.session.testscollected

    behauptet: list[tuple[str, int]] = []
    muster = re.compile(r"(\d{2,4})\s+Tests\b")
    for pfad in [WURZEL / "README.md", DOCS / "01_ARCHITEKTUR.md"]:
        for zeile in pfad.read_text(encoding="utf-8").splitlines():
            m = muster.search(zeile)
            if m:
                behauptet.append((pfad.name, int(m.group(1))))

    falsch = [(name, zahl) for name, zahl in behauptet if zahl != ist]
    assert not falsch, (
        f"Tatsächlich gesammelt: {ist}. Diese Angaben stimmen nicht mehr: {falsch}"
    )


@_OHNE_DOCS
def test_stand_dokument_nennt_die_aktuelle_version() -> None:
    """Das Dokument, das laut eigener Ansage zuerst gelesen wird, darf nicht
    fünf Versionen hinterherhinken."""
    from moneten import __version__

    kopf = (DOCS / "_STAND_UND_TODO.md").read_text(encoding="utf-8")[:1500]
    assert __version__ in kopf, (
        f"docs/_STAND_UND_TODO.md nennt im Kopf nicht die aktuelle Version {__version__}"
    )


@_OHNE_DOCS
def test_changelog_hat_einen_eintrag_zur_aktuellen_version() -> None:
    from moneten import __version__

    text = (DOCS / "changelog.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text, (
        f"Kein Changelog-Eintrag für die aktuelle Version {__version__}"
    )


def test_die_version_steht_nur_an_einer_stelle() -> None:
    """``pyproject.toml`` darf keine eigene Versionsnummer tragen.

    Sie stand dort ein zweites Mal und blieb bei 0.49.0 stehen, waehrend die App
    sich als 0.66.0 meldete — im Build-Protokoll las man dann „bilanz-0.49.0"
    (``bilanz`` war der frühere Name des Projekts).
    Folgenlos, aber eine Zahl, die luegt, ist eine Zahl zu viel.

    Geprueft wird die QUELLE, nicht das Ergebnis: ``importlib.metadata`` liest
    die Angabe aus der Installation, und die haengt im editierbaren Umfeld daran,
    wann zuletzt installiert wurde. Der Vertrag ist „eine Stelle", nicht „zwei
    gleiche Stellen".
    """
    text = (WURZEL / "pyproject.toml").read_text(encoding="utf-8")
    kopf = text.split("[build-system]")[0]
    eigene = [z for z in kopf.splitlines() if re.match(r"\s*version\s*=\s*\S", z)]
    assert not eigene, f"pyproject.toml traegt eine eigene Version: {eigene}"
    assert 'dynamic = ["version"]' in kopf, "pyproject.toml leitet die Version nicht ab"
