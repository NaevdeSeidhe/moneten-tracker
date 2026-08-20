"""Ein Rechnungslayout als DATEN statt als Code.

**Warum.** Eine Rechnung mit einzeln aufgeschlüsselten Positionen zu lesen
verlangt Wissen über genau dieses Layout: wo die Positionen anfangen, wie die
Abschnitte heissen, welche Zeile einen Abschnitt beendet, wie der Rechnungskopf
aussieht. Dieses Wissen stand als Regex-Konstanten im Parser — mit dem Namen des
Anbieters im Funktionsnamen. Damit war jeder weitere Anbieter eine Code-Änderung,
und der Quelltext verriet, bei wem der Autor Kunde ist.

Jetzt ist ein Anbieter eine ``.toml``-Datei. Wer eine andere Rechnung einlesen
will, legt eine zweite Datei daneben. Der Parser bleibt derselbe.

**Zwei Quellen, mit Absicht getrennt:**

* mitgeliefert (``src/moneten/anbieter/``) — Beispielprofile, die zeigen, wie
  eine Datei aussieht, und an denen die Tests laufen.
* eigene (``MONETEN_ANBIETER_DIR``, standardmässig ``data/anbieter``) — die
  eigenen Anbieter. Sie liegen bei den Daten, nicht beim Programm, und wandern
  darum weder ins Abbild noch in ein Repository.

Bei gleichem ``slug`` gewinnt die eigene Datei: sonst könnte eine mitgelieferte
Vorlage eine gepflegte Fassung überschreiben.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

# Mitgelieferte Beispielprofile — neben dem Paket, nicht bei den Daten.
MITGELIEFERT = Path(__file__).resolve().parent.parent / "anbieter"


class ProfilFehler(ValueError):
    """Ein Profil ist unbrauchbar — mit dem Grund im Text.

    Eine eigene Ausnahme statt „einfach überspringen": ein stillschweigend
    ignoriertes Profil sieht aus wie „die Rechnung wurde nicht erkannt", und
    dann sucht man den Fehler im Beleg statt in der Datei.
    """


@dataclass(frozen=True)
class Anbieterprofil:
    """Alles, was der Parser über EIN Rechnungslayout wissen muss."""

    slug: str
    name: str
    # Wort, an dem der Anbieter im schon gelesenen Belegtext erkannt wird —
    # ein billiger Vorfilter, bevor das PDF ein zweites Mal spaltenweise
    # geoeffnet wird. Ohne ihn zahlte jede Ladenquittung im selben Ordner
    # die teure Extraktion einer Rechnung mit, die sie nie ist.
    stichwort: str
    anker: str
    abschnitte: frozenset[str]
    ende: tuple[str, ...]
    monat: re.Pattern[str]
    total: re.Pattern[str]
    rundung: re.Pattern[str] | None
    laufzeit: re.Pattern[str] | None
    toleranz: Decimal
    # Kategorie, in der die Zahlungen an diesen Anbieter gebucht sein sollten.
    # Sie verknüpft die Verlaufsreihe mit dem Soll/Ist-Abgleich; ``None`` heisst
    # „ein Abgleich ergibt hier keinen Sinn".
    kategorie: str | None
    notiz: str
    # Woher das Profil stammt. Daran hängt eine Entscheidung: nur EIGENE Profile
    # legen eine Verlaufsreihe an. Ein mitgeliefertes Beispiel soll keine
    # Demo-Reihe in eine frische Installation legen.
    herkunft: Path


def _muster(roh: dict, schluessel: str, gruppen: int, quelle: Path) -> re.Pattern[str]:
    """Übersetzt einen Eintrag in ein geprüftes Muster.

    Die Gruppenzahl wird MITGEPRÜFT. Ein Muster mit der falschen Anzahl Klammern
    scheitert sonst erst mitten im Einlesen einer Rechnung, mit einem
    ``IndexError`` weit weg von seiner Ursache.
    """
    wert = roh.get(schluessel)
    if not isinstance(wert, str) or not wert:
        raise ProfilFehler(f"{quelle.name}: [muster].{schluessel} fehlt oder ist leer")
    try:
        muster = re.compile(wert)
    except re.error as fehler:
        raise ProfilFehler(f"{quelle.name}: [muster].{schluessel} ist kein gültiger Ausdruck ({fehler})") from fehler
    if muster.groups != gruppen:
        raise ProfilFehler(
            f"{quelle.name}: [muster].{schluessel} braucht genau {gruppen} Klammergruppe(n), "
            f"hat aber {muster.groups}"
        )
    return muster


def _optionales_muster(roh: dict, schluessel: str, gruppen: int, quelle: Path) -> re.Pattern[str] | None:
    return _muster(roh, schluessel, gruppen, quelle) if roh.get(schluessel) else None


def lies_profil(pfad: Path) -> Anbieterprofil:
    """Liest EINE Profildatei und prüft sie vollständig."""
    try:
        roh = tomllib.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as fehler:
        raise ProfilFehler(f"{pfad.name}: nicht lesbar ({fehler})") from fehler

    for pflicht in ("slug", "anker", "abschnitte", "ende"):
        if not roh.get(pflicht):
            raise ProfilFehler(f"{pfad.name}: '{pflicht}' fehlt oder ist leer")

    muster = roh.get("muster")
    if not isinstance(muster, dict):
        raise ProfilFehler(f"{pfad.name}: der Abschnitt [muster] fehlt")

    try:
        toleranz = Decimal(str(roh.get("toleranz", "0")))
    except InvalidOperation as fehler:
        raise ProfilFehler(f"{pfad.name}: 'toleranz' ist keine Zahl") from fehler

    return Anbieterprofil(
        slug=str(roh["slug"]),
        name=str(roh.get("name", roh["slug"])),
        stichwort=str(roh.get("stichwort", roh.get("name", roh["slug"]))).lower(),
        anker=str(roh["anker"]),
        abschnitte=frozenset(str(a) for a in roh["abschnitte"]),
        ende=tuple(str(e) for e in roh["ende"]),
        # Zwei Gruppen: Monatsname und Jahr. Eine Gruppe: der Betrag.
        monat=_muster(muster, "monat", 2, pfad),
        total=_muster(muster, "total", 1, pfad),
        rundung=_optionales_muster(muster, "rundung", 1, pfad),
        # Ohne Klammergruppe: der Ausdruck wird nur entfernt, nicht ausgelesen.
        laufzeit=_optionales_muster(muster, "laufzeit", 0, pfad),
        toleranz=toleranz,
        kategorie=str(roh["kategorie"]) if roh.get("kategorie") else None,
        notiz=str(roh.get("notiz", "Monatsrechnung mit allen Positionen.")),
        herkunft=pfad,
    )


def ist_eigenes(profil: Anbieterprofil) -> bool:
    """Stammt das Profil aus dem eigenen Ordner und nicht aus der Mitlieferung?

    Als Funktion und nicht als Feld: die Antwort ergibt sich aus dem Ort, und ein
    zweites Feld daneben könnte ihm widersprechen.
    """
    return profil.herkunft.parent != MITGELIEFERT


def lade_profile(eigene: Path | None) -> dict[str, Anbieterprofil]:
    """Alle Profile, ``slug`` → Profil. Eigene schlagen mitgelieferte.

    Ein kaputtes Profil hält die App NICHT an: sie soll auch dann starten, wenn
    jemand beim Bearbeiten einer Datei einen Fehler macht. Es wird aber
    protokolliert — stillschweigend zu übergehen wäre genau die lautlose Sorte
    Fehler, die man später am Beleg sucht.
    """
    profile: dict[str, Anbieterprofil] = {}
    for ordner in (MITGELIEFERT, eigene):
        if ordner is None or not Path(ordner).is_dir():
            continue
        for pfad in sorted(Path(ordner).glob("*.toml")):
            try:
                profil = lies_profil(pfad)
            except ProfilFehler as fehler:
                logger.warning("Anbieterprofil übersprungen: %s", fehler)
                continue
            profile[profil.slug] = profil
    return profile
