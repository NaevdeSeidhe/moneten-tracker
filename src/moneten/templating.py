"""Zentrale Jinja2-Templating-Konfiguration.

Liegt im Paket-Root, damit Router und ``main.py`` dasselbe ``templates``-Objekt
importieren können — sonst würden Filter und globale Variablen doppelt
registriert werden.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from moneten import __version__, themes
from moneten.config import settings
from moneten.dates import local_now
from moneten.icons import ICON_NAMES
from moneten.services.zeitachse import bis_heute as _zeit_bis_heute
from moneten.services.zeitachse import marken as _zeit_marken
from moneten.themes import Theme

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Deutsche Wochentags-/Monatsnamen — bewusst manuell statt locale-abhängig,
# damit die Anzeige auf jedem System (auch im Container) identisch ist.
# MONATE ist öffentlich (kein führender Unterstrich), weil mehrere Router den
# deutschen Monatsnamen brauchen — so existiert die Liste nur an EINER Stelle.
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def heute_label() -> str:
    """Heutiges Datum als ``Samstag, 30. Mai 2026`` — für die Brand-Sub im Header."""
    now = local_now()
    return f"{WOCHENTAGE[now.weekday()]}, {now.day}. {MONATE[now.month - 1]} {now.year}"


def letzter_import() -> str:
    """Datum des letzten Bank-Imports für die Footer-Statuszeile.

    Liest den jüngsten ``ImportBatch`` direkt aus der DB (Footer ist auf jeder
    Seite, daher als Template-Global statt über jeden Router). Imports auf einer
    privaten Single-User-App sind selten — eine Mini-Query pro Seitenaufruf ist
    unkritisch. Schlägt etwas fehl (z.B. DB beim Start noch leer), zeigen wir
    neutral „noch keiner", damit der Footer nie eine Seite bricht.
    """
    try:
        from sqlalchemy import select

        from moneten.db.models import ImportBatch
        from moneten.db.session import SessionLocal

        with SessionLocal() as db:
            ts = db.scalar(
                select(ImportBatch.imported_at).order_by(ImportBatch.imported_at.desc()).limit(1)
            )
        return ts.strftime("%d.%m.%Y") if ts else "noch keiner"
    except Exception:  # noqa: BLE001 — Footer darf nie eine Seite zum Absturz bringen
        return "noch keiner"


def theme_of(user) -> Theme:  # noqa: ANN001 — User oder None, bewusst tolerant
    """Aktives Theme als Objekt — auch auf Seiten OHNE eingeloggten User (Login).

    Kapselt den Fallback an einer Stelle, damit base.html nicht bei jedem neuen
    Theme angefasst werden muss: Hintergrundfarbe und Helligkeit kommen aus der
    Registry (:mod:`moneten.themes`).
    """
    return themes.get(getattr(user, "preferred_theme", None))


# Global in allen Templates verfügbar, ohne dass jede Route es einzeln übergeben muss.
templates.env.globals["app_version"] = __version__


def _statik_fingerabdruck() -> str:
    """Kurzer Hash über CSS und JS — der Cache-Schluessel der statischen Dateien.

    WARUM NICHT DIE VERSIONSNUMMER: genau daran ist es gescheitert. Die Links in
    ``base.html`` trugen ``?v={{ app_version }}``. Wird die Version bei einem
    Deploy nicht erhoeht — und das passiert bei jeder Zwischenfassung —, bleibt
    der Schluessel gleich, und der Browser behaelt sein zwischengespeichertes
    Stylesheet. Ergebnis: neue Vorlagen, altes CSS. Auf dem Schirm sah das aus
    wie kaputte Seiten (SVG-Kreise in schwarzer Standardfuellung, Legenden als
    nackte Wortketten), und keine noch so gruene Testsuite konnte das zeigen —
    die Dateien im Repo waren ja richtig.

    Der Hash aendert sich bei jeder Aenderung am Inhalt und nur dann. Kein
    Mensch muss mehr daran denken.
    """
    import hashlib

    h = hashlib.sha256()
    for name in ("css/fonts.css", "css/skins.css", "css/theme.css", "js/app.js"):
        datei = Path(__file__).parent / "static" / name
        if datei.is_file():
            h.update(datei.read_bytes())
    return h.hexdigest()[:12]


# Einmal beim Laden gerechnet: die Dateien aendern sich zur Laufzeit nicht, und
# bei jedem Seitenaufbau vier Dateien zu hashen waere Verschwendung.
templates.env.globals["statik_v"] = _statik_fingerabdruck()

# Zeitachse der Verlaufsdiagramme. Als Globals und nicht als Filter: beide
# Diagramme brauchen dieselben Marken, und ein Filter haette den ersten
# Parameter zum Empfaenger gemacht, was hier nichts erklaert.
templates.env.globals["zeit_marken"] = _zeit_marken
templates.env.globals["zeit_bis_heute"] = _zeit_bis_heute
# Karenz fuer die Sperre beim Zurueckkehren — app.js liest sie aus dem Markup,
# damit die Zahl nur an EINER Stelle steht (config.py).
templates.env.globals["lock_grace"] = settings.session_return_grace_seconds
templates.env.globals["app_name"] = "Moneten-Tracker"
templates.env.globals["heute_label"] = heute_label
templates.env.globals["letzter_import"] = letzter_import
templates.env.globals["theme_of"] = theme_of
templates.env.globals["THEMES"] = themes.THEMES


def kurve(koordinaten: list[dict]) -> str:
    """Weicher SVG-Pfad durch Punkte mit ``x``/``y`` — für die Verlaufsdiagramme.

    Nimmt die im Template gerechneten Bildschirmkoordinaten entgegen und gibt
    das fertige ``d``-Attribut zurück. Die Glättung selbst steht in
    :mod:`moneten.services.charts`, damit Sparklines und Verlaufsseite dieselbe
    Rundung verwenden statt zweier verschiedener Kurven.

    ``klemmen=True``: die Punkte liegen hier auf einer ECHTEN Zeitachse mit sehr
    ungleichen Abständen — zwischen zwei Werten kann ein Monat oder ein Jahr
    liegen. Ungeklemmt schlüge die Kurve dabei Schlaufen und liefe durch
    Beträge, die es nie gab.
    """
    from moneten.services.charts import curve_path

    return curve_path([(k["x"], k["y"]) for k in koordinaten], klemmen=True)


templates.env.globals["kurve"] = kurve


# Echtes Minuszeichen (U+2212) statt Bindestrich. Es ist so breit wie das Plus
# und liegt auf der Höhe der Ziffern; der Bindestrich sitzt tiefer und schmaler
# und liest sich in einer Zahlenspalte wie ein Trennstrich. Die Templates setzen
# es an den Stellen, an denen sie das Vorzeichen selbst schreiben („−{{ betrag }}")
# — die Filter taten es nicht, und in derselben Zeile standen dann „−900" und
# „-150" nebeneinander.
MINUS = "−"


def _minus(text: str) -> str:
    return text.replace("-", MINUS, 1) if text.startswith("-") else text


def chf(value: Decimal | float | int | None) -> str:
    """Formatiert einen Betrag wie ``CHF 1'234.50``.

    Schweizer Konvention: Apostroph als Tausendertrenner, Punkt als Dezimal.
    """
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    formatted = _minus(f"{value:,.2f}".replace(",", "'"))
    return f"CHF {formatted}"


def chf_kurz(value: Decimal | float | int | None) -> str:
    """Betrag ohne Währung und ohne Rappen — ``1'234``, Null als ``–``.

    Für dichte Listen (Budget-Zeilen, Gruppenköpfe): dort steht die Währung
    schon im Kartenkopf, und `CHF 0.00` hundertfach wiederholt ist genau die
    Zahlenflut, die die Seite auf dem Handy unlesbar macht. Ein Gedankenstrich
    liest sich als „nichts" schneller als eine ausgeschriebene Null.
    """
    if value is None:
        return "–"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value == 0:
        return "–"
    return _minus(f"{value:,.0f}".replace(",", "'"))


def chf_wert(value: Decimal | float | int | None) -> str:
    """Betrag OHNE Währungskürzel, aber MIT Rappen — ``1'234.50``.

    Für die Buchungsliste: dort steht ``CHF`` in jeder einzelnen Zeile und
    kostet auf 375px rund 34px Breite, die der Beschreibung fehlen. Die Rappen
    bleiben — anders als bei :func:`chf_kurz` wäre eine Einzelbuchung ohne sie
    schlicht falsch. Die Währung nennt die Leitzahl der Seite einmal.
    """
    if value is None:
        return "–"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value:,.2f}".replace(",", "'").replace("-", "−")


# Bank-Präfixe, die in praktisch jeder importierten Zeile stehen und nichts
# unterscheiden. Längster Treffer zuerst, damit „e-banking auftrag an " vor
# „e-banking " greift.
_DESC_PREFIXE = (
    "e-banking auftrag an ", "e-banking auftrag ", "e-banking ",
    "kauf/dienstleistung ", "online-einkauf ", "onlinekauf ",
    "kartenzahlung ", "dauerauftrag ", "lastschrift ", "gutschrift ",
    "belastung ", "auftrag an ", "einkauf ", "zahlung ", "kauf ", "lsv ",
)
# Bindewörter, die nach dem Abschneiden übrig bleiben würden: aus
# „Zahlung an Penelope" darf nicht „an Penelope" werden. Nur direkt nach einem
# entfernten Präfix angewandt — eine Beschreibung DARF mit „Von …" beginnen.
_DESC_BINDEWORT = re.compile(r"^(?:an|bei|für|fuer|von|vom|zu|zugunsten)(?:\s+|$)", re.IGNORECASE)
# Angehängte Uhrzeit bewusst NUR mit Doppelpunkt: „Migros 12.50" wäre sonst ein
# Treffer und der Betrag verschwände aus dem Text.
_DESC_ZEIT = re.compile(r"[\s,;·]*\d{1,2}:\d{2}(?::\d{2})?\s*$")
_DESC_DATUM = re.compile(r"[\s,;·]*\d{1,2}\.\d{1,2}\.(?:\d{2}|\d{4})\s*$")
_DESC_KARTE = re.compile(r"[\s,;·]*karten?\s*(?:nr\.?|nummer)?\s*[x*\d]{3,}\s*$", re.IGNORECASE)


def desc_kurz(value: str | None) -> str:
    """Kürzt einen Buchungstext fürs AUGE — die gespeicherte ``description``
    bleibt unangetastet.

    Der Text kommt 1:1 von der Bank (``camt053_parser._entry_description``). In
    der Liste bleiben auf 375px rund 19 Zeichen sichtbar; allein
    „E-Banking Auftrag an " belegt 21 davon, der Empfängername wird also
    garantiert abgeschnitten. Gesucht und gefiltert wird weiterhin auf dem
    Original, und die Zeile trägt es als ``title``.

    Bleibt nach dem Kürzen nichts übrig, gewinnt der Originaltext — lieber
    abgeschnitten als leer.
    """
    s = " ".join((value or "").split())
    if not s:
        return "(ohne Beschreibung)"
    original = s
    low = s.lower()
    for p in _DESC_PREFIXE:
        if low.startswith(p):
            s = _DESC_BINDEWORT.sub("", s[len(p):].lstrip(" -–—:,"))
            break
    for rx in (_DESC_ZEIT, _DESC_DATUM, _DESC_KARTE):
        s = rx.sub("", s)
    return s.strip(" -–—:,") or original


def fromjson(value: str | None) -> dict:
    """Parst einen JSON-String zu einem dict (leeres dict bei None/Fehler)."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {}


templates.env.filters["chf"] = chf
templates.env.filters["chf_kurz"] = chf_kurz
templates.env.filters["chf_wert"] = chf_wert
templates.env.filters["desc_kurz"] = desc_kurz
templates.env.filters["fromjson"] = fromjson


# Verfügbare Icon-IDs — zentral in moneten.icons gepflegt (jede muss als
# <symbol id="i-NAME"> in partials/icon_sprite.html existieren). Unbekannte/leere
# Namen fallen auf das generische „tag"-Icon zurück.
_ICON_NAMES = ICON_NAMES


def icon(name: str | None, cls: str = "cat-icon") -> Markup:
    """Rendert ein Kategorie-Icon aus der inline-Sprite (offline, currentColor).

    Unbekannte/leere Namen fallen sicher auf das generische „tag"-Icon zurück,
    damit nie ein leeres <use> entsteht.
    """
    sym = name if (name and name in _ICON_NAMES) else "tag"
    return Markup(
        f'<svg class="{cls}" aria-hidden="true" focusable="false"><use href="#i-{sym}"></use></svg>'
    )


templates.env.globals["icon"] = icon
