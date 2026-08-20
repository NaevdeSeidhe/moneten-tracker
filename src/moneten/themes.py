"""Theme-Registry — die eine Quelle der Wahrheit für verfügbare Farbwelten.

Ein Theme ist EIN Name (einachsig). Früher gab es zwei Achsen: ``preferred_theme``
(dark|light) **und** ein Boolean für einen Zweit-Skin. Das liess sich nicht auf
mehr als zwei Farbwelten erweitern — „Reactor" ist ja kein Modus von Dark,
sondern eine eigene Welt.

Ein neues Theme hinzufügen:
  1. Token-Block ``[data-theme="name"]`` in ``static/css/skins.css`` anlegen.
  2. Hier einen Eintrag ergänzen.
Keine Migration nötig — die Spalte ist ein freier String.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THEME = "dark"


@dataclass(frozen=True)
class Theme:
    """Ein auswählbares Erscheinungsbild."""

    key: str
    label: str
    dark: bool          # Grundhelligkeit — steuert u.a. die PWA-Statusleiste
    bg: str             # Seitenhintergrund; wird INLINE gesetzt, damit der erste
                        # Paint schon stimmt (kein Hell-Aufblitzen auf Mobile)
    note: str = ""      # kurze Beschreibung für die Einstellungen
    structural: bool = False  # bringt eigene Geometrie/Schrift mit (eigene CSS-Datei)


THEMES: tuple[Theme, ...] = (
    Theme("dark", "Dunkel", dark=True, bg="#1A1917", note="Warmes Anthrazit — Standard."),
    Theme("light", "Hell", dark=False, bg="#F4F1EA", note="Sand auf Cream."),
    Theme("nord", "Nord", dark=True, bg="#2E3440", note="Kühles Blaugrau."),
    Theme("synthwave", "Synthwave", dark=True, bg="#262335",
          note="Neon auf Violett-Nacht — Pink und Cyan."),
    Theme("melange", "Melange", dark=True, bg="#292522",
          note="Warmes, gedecktes Braun-Beige."),
    Theme("ayu-hell", "Ayu Hell", dark=False, bg="#FCFCFC",
          note="Sehr helles Grau-Weiss mit Orange."),
)

_BY_KEY = {t.key: t for t in THEMES}


def get(key: str | None) -> Theme:
    """Theme zum Schlüssel — unbekannte/leere Werte fallen auf den Default zurück.

    Wichtig für Robustheit: In der DB steht ein freier String. Wird ein Theme
    aus ``skins.css`` entfernt, darf die App nicht kaputtgehen.
    """
    return _BY_KEY.get((key or "").strip().lower(), _BY_KEY[DEFAULT_THEME])


def is_valid(key: str | None) -> bool:
    return (key or "").strip().lower() in _BY_KEY
