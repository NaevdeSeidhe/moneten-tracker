"""Was ein Fremder liest, muss lesbar und vollständig sein.

Drei Sorten Schaden, die alle drei wirklich passiert sind und die alle drei
niemandem auffallen, weil sie nichts kaputtmachen — sie stehen nur da:

* **Steuerzeichen im Text.** In ``.env.example`` stand jahrelang ``C:<BEL>pp``
  statt ``C:\\app`` — ein verschluckter Backslash, aus dem beim Schreiben ein
  Klingelzeichen wurde. Der Hinweis, der vor einer verlegten Datenbank schützen
  sollte, nannte einen Pfad, den es nicht gibt, und liess das Terminal piepen.
* **Doppelt kodierte Zeichen.** ``base.html`` trug 17 Stellen, an denen aus
  einem Anführungszeichen oder einem Pfeil drei Zeichen Buchstabensalat
  geworden waren: einmal als cp1252 gelesenes UTF-8. Das steht im
  Seitenquelltext jeder ausgelieferten Seite. Beispiele stehen hier bewusst
  NICHT wörtlich — dieser Wächter würde sonst seine eigene Beschreibung melden.
* **Eine Konfiguration, die nur halb dokumentiert ist.** Drei von fünfzehn
  Schaltern standen in keiner der beiden Dateien, die ein Neuling liest —
  darunter die Zeitzone, ohne die die App nachts den Vortag zeigt.

Bewusst ohne Ausnahmeliste. Eine Liste erlaubter Dateien deckt später auch das,
was neu dazukommt.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]

#: Alles, was mitgeliefert wird und Text ist.
ENDUNGEN = {".py", ".html", ".css", ".js", ".md", ".toml", ".sh", ".ini",
            ".example", ".webmanifest", ".yml", ".txt"}
NICHT = {".venv", "data", "_devdata", "__pycache__", ".git", ".pytest_cache",
         "node_modules", ".ruff_cache"}

#: Erlaubt sind Zeilenumbruch, Wagenrücklauf und Tabulator — sonst nichts unter 0x20.
_STEUERZEICHEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: Spuren von einmal falsch gelesenem UTF-8 (cp1252/Latin-1).
_DOPPELT_KODIERT = re.compile("[\u00e2\u00c3]\\S|\u00c2[ \u00a0]")


def _textdateien() -> list[Path]:
    return [
        p for p in sorted(WURZEL.rglob("*"))
        if p.is_file() and p.suffix.lower() in ENDUNGEN
        and not any(teil in NICHT for teil in p.relative_to(WURZEL).parts)
    ]


def test_keine_steuerzeichen_im_quelltext() -> None:
    """Ein unsichtbares Zeichen ist schlimmer als ein falsches: man sieht es nicht.

    Gefunden wurde der Fall in ``.env.example``; entstanden ist er beim
    Schreiben durch ein Werkzeug, das eine Ebene Backslash verschluckt hat.
    """
    treffer = []
    for p in _textdateien():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            treffer.append(f"{p.relative_to(WURZEL)}: nicht als UTF-8 lesbar")
            continue
        for nr, zeile in enumerate(text.splitlines(), 1):
            if m := _STEUERZEICHEN.search(zeile):
                treffer.append(
                    f"{p.relative_to(WURZEL)}:{nr}: {hex(ord(m.group()))} in {zeile.strip()[:60]!r}"
                )
    assert not treffer, "Steuerzeichen im Text:\n" + "\n".join(treffer)


def test_keine_doppelt_kodierten_zeichen() -> None:
    """Buchstabensalat statt Umlaut — UTF-8, das einmal als cp1252 gelesen wurde.

    Betroffen war ``base.html``, also ausgerechnet die Datei, deren Kommentare
    als Markup an jeden Browser gehen.
    """
    treffer = []
    for p in _textdateien():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # der andere Test meldet das
        for nr, zeile in enumerate(text.splitlines(), 1):
            if m := _DOPPELT_KODIERT.search(zeile):
                treffer.append(
                    f"{p.relative_to(WURZEL)}:{nr}: {m.group()!r} in {zeile.strip()[:60]!r}"
                )
    assert not treffer, (
        "Doppelt kodierte Zeichen (UTF-8 einmal als cp1252 gelesen):\n" + "\n".join(treffer)
    )


#: Was ausgeliefert oder ausgeführt wird. Bewusst NICHT die Notizen unter
#: ``docs/`` und der interne README: die schreibt Obsidian mit BOM, dort richtet
#: er keinen Schaden an, und ein Wächter, der bei jedem Speichern rot wird, wird
#: abgeschaltet statt beachtet.
_AUSGELIEFERT = ("src", "scripts", "alembic")


def test_kein_byte_order_mark() -> None:
    """Ein BOM vor ``<!DOCTYPE html>`` gehört in keine Vorlage.

    Genau eine ausgelieferte Datei trug einen: ``base.html``, die Vorlage, die
    jede Seite umschliesst. Der Browser verzeiht es; in einer Antwort hat ein
    unsichtbares Zeichen trotzdem nichts verloren.
    """
    mit_bom = [
        str(p.relative_to(WURZEL)) for p in _textdateien()
        if p.relative_to(WURZEL).parts[0] in _AUSGELIEFERT
        and p.read_bytes()[:3] == b"\xef\xbb\xbf"
    ]
    assert not mit_bom, f"Ausgelieferte Dateien mit BOM: {mit_bom}"


# ---------------------------------------------------------------------------
# Die Konfiguration muss vollständig dokumentiert sein
# ---------------------------------------------------------------------------
def _felder() -> set[str]:
    from moneten.config import Settings

    return {f"MONETEN_{name.upper()}" for name in Settings.model_fields}


def test_jeder_schalter_steht_in_der_env_beispieldatei() -> None:
    """Ein Schalter, den niemand findet, ist keiner.

    ``MONETEN_TIMEZONE`` fehlte hier, nachdem es eingebaut worden war — und ohne
    ihn zeigt die App auf einem Server in UTC nachts den Vortag.
    """
    text = (WURZEL / ".env.example").read_text(encoding="utf-8")
    genannt = set(re.findall(r"MONETEN_[A-Z_]+", text))
    fehlend = sorted(_felder() - genannt)
    assert not fehlend, (
        f"Diese Schalter stehen in keiner Zeile von .env.example: {', '.join(fehlend)}"
    )


def test_jeder_schalter_steht_im_veroeffentlichten_readme() -> None:
    """Dieselbe Prüfung für die Datei, die ein Fremder zuerst liest.

    Der README liegt im Arbeitsordner bei den Vorlagen und im Export in der
    Wurzel. Liegt er nirgends, gibt es nichts abzugleichen.
    """
    orte = [
        WURZEL / "README.md" if (WURZEL / "THIRD-PARTY-NOTICES.md").is_file() else None,
        WURZEL.parent / "veroeffentlichen" / "vorlagen" / "README.md",
    ]
    pfad = next((p for p in orte if p is not None and p.is_file()), None)
    if pfad is None:
        pytest.skip("Kein veröffentlichter README daneben — nichts abzugleichen")

    genannt = set(re.findall(r"MONETEN_[A-Z_]+", pfad.read_text(encoding="utf-8")))
    fehlend = sorted(_felder() - genannt)
    assert not fehlend, (
        f"Diese Schalter fehlen in der Konfigurations-Tabelle von {pfad.name}: "
        f"{', '.join(fehlend)}"
    )


#: Schalter, die NICHT aus ``config.py`` kommen, sondern vom Container-Entrypoint
#: gelesen werden (``scripts/entrypoint.sh``). Namentlich aufgeführt und nicht
#: als Muster: ein neuer, unbekannter Schalter soll weiterhin auffallen.
_VOM_ENTRYPOINT = {
    "MONETEN_UID", "MONETEN_GID", "MONETEN_DATA_DIR", "MONETEN_HOME",
    "MONETEN_FORWARDED_ALLOW_IPS",
}


def test_die_beispieldatei_nennt_keine_erfundenen_schalter() -> None:
    """Die Gegenrichtung: ein Schalter, den es nicht mehr gibt, führt in die Irre.

    Er sieht aus wie eine Einstellung, wird gesetzt — und wirkt nicht.
    """
    text = (WURZEL / ".env.example").read_text(encoding="utf-8")
    genannt = set(re.findall(r"^#?\s*(MONETEN_[A-Z_]+)=", text, re.MULTILINE))
    unbekannt = sorted(genannt - _felder() - _VOM_ENTRYPOINT)
    assert not unbekannt, f"Unbekannte Schalter in .env.example: {', '.join(unbekannt)}"


def test_pyproject_und_beispieldatei_sind_utf8() -> None:
    """Gegenprobe zum Kodierungs-Test an den zwei Dateien, die zuerst gelesen werden."""
    for name in ("pyproject.toml", ".env.example"):
        roh = (WURZEL / name).read_bytes()
        roh.decode("utf-8")  # wirft, wenn nicht
    tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Zeilenenden: der Fehler, der erst auf dem NAS ausbricht
# ---------------------------------------------------------------------------
#: Dateien, die im Container ausgeführt oder von Docker gelesen werden.
_LINUX_DATEIEN = ("scripts/backup.sh", "scripts/restore.sh", "scripts/entrypoint.sh",
                  "Dockerfile", ".dockerignore")


def test_container_dateien_haben_unix_zeilenenden() -> None:
    """CRLF in einem Shell-Skript ist unter Windows unsichtbar und tödlich.

    **Der gemessene Fall.** Eine Bearbeitung mit ``pathlib.write_text`` wandelt
    unter Windows jedes ``\n`` in ``\r\n`` um — die Datei sieht danach in jedem
    Editor gleich aus. Auf dem NAS liest die Shell dann ``set -euo pipefail\r``
    und bricht ab mit::

        line 17: set: pipefail: invalid option name

    Das passierte mitten im Deploy, nach dem Hochladen, im Backup-Schritt. Kein
    Test hätte es hier gemerkt: unter Windows läuft keines dieser Skripte.
    """
    kaputt = []
    for name in _LINUX_DATEIEN:
        pfad = WURZEL / name
        if not pfad.is_file():
            continue
        roh = pfad.read_bytes()
        if b"\r\n" in roh:
            kaputt.append(f"{name} ({roh.count(chr(13).encode() + chr(10).encode())} Zeilen)")
    assert not kaputt, (
        "Diese Dateien tragen CRLF und scheitern im Container:\n  " + "\n  ".join(kaputt)
        + "\n\nZurücksetzen: Datei binär lesen, \r\n durch \n ersetzen, binär schreiben."
    )


def test_shell_skripte_beginnen_mit_einer_shebang() -> None:
    """Gegenprobe aus derselben Ecke: ohne Shebang entscheidet der Aufrufer,
    welche Shell es liest — und ``sh`` kennt ``pipefail`` nicht."""
    ohne = []
    for name in _LINUX_DATEIEN:
        if not name.endswith(".sh"):
            continue
        pfad = WURZEL / name
        if pfad.is_file() and not pfad.read_bytes().startswith(b"#!"):
            ohne.append(name)
    assert not ohne, f"Ohne Shebang: {ohne}"
