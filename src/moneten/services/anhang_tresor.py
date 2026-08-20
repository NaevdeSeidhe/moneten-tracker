"""Verschlüsselung der Dateien, die die App selbst ablegt (Beleg-Fotos).

**Warum.** Die Datenbank liegt verschlüsselt (SQLCipher). Ein abfotografierter
Kassenzettel zeigt dieselben Daten — Händler, Datum, Summe, jede Position — und
gehört deshalb hinter denselben Schutz und nicht als gewöhnliche Bilddatei
daneben: der Schutz der Datenbank ist sonst nur so stark wie der Ordner neben
ihr.

**Was hier passiert.** Ist ein ``MONETEN_DB_KEY`` gesetzt — dieselbe Bedingung,
unter der auch die Datenbank verschlüsselt wird —, werden diese Dateien mit
AES-256-GCM verschlüsselt geschrieben. Ohne Schlüssel (Entwicklung, Tests)
bleibt alles wie bisher: eine App, die im Klartext läuft, gewinnt nichts durch
verschlüsselte Beilagen.

**Der Schlüssel wird abgeleitet, nicht wiederverwendet.** ``MONETEN_DB_KEY`` ist
eine Passphrase für SQLCipher. Sie hier direkt als AES-Schlüssel zu nehmen wäre
zweierlei falsch: sie hat keine feste Länge, und ein Schlüssel sollte genau eine
Aufgabe haben. HKDF-SHA256 macht daraus 32 Bytes für genau diesen Zweck; wer den
einen Schlüssel bekommt, hat damit nicht den anderen.

**Format** (bewusst selbsterklärend, damit auch in fünf Jahren erkennbar ist,
was da liegt)::

    MNTC1\\x00 | 12 Byte Nonce | Geheimtext + GCM-Siegel (16 Byte)

GCM siegelt mit: eine veränderte Datei entschlüsselt nicht, sie **scheitert**.
Das ist der Unterschied zu reiner Verschleierung — niemand kann ein Bild
unbemerkt austauschen oder Bytes darin drehen.

**Kein Dateiname im Siegel.** GCM könnte den Dateinamen mitsiegeln, dann wäre
auch das Umbenennen erkennbar. Dagegen spricht der Alltag: eine Rücksicherung,
die Namen normalisiert, oder ein Umzug des Ordners würde jede Datei unlesbar
machen — ein Schaden, der viel wahrscheinlicher ist als der Angriff, den es
verhindert. Die Namen sind Zeitstempel und tragen selbst keine Aussage.

**Alte Dateien bleiben lesbar.** :func:`lesen` erkennt am Kopf, was vorliegt,
und gibt Klartext-Dateien unverändert zurück. Sonst wäre jede Umstellung ein
Alles-oder-nichts, bei dem ein Abbruch mitten in der Wandlung den Bestand
zerreisst. Umgewandelt wird mit ``scripts/anhaenge_verschluesseln.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from moneten.config import settings

MAGIE = b"MNTC1\x00"
NONCE_LAENGE = 12

# Feste Zutaten der Ableitung. Beide gehören zum Format: ändert man sie, sind
# alle bestehenden Dateien unlesbar. Darum stehen sie hier und nicht in der
# Konfiguration.
_HKDF_SALZ = b"moneten-anhang-hkdf-v1"
_HKDF_ZWECK = b"beleg-fotos"


class TresorFehler(RuntimeError):
    """Eine verschlüsselte Datei liess sich nicht öffnen."""


def _schluessel() -> bytes | None:
    """32 Bytes aus dem Datenbank-Schlüssel — oder ``None``, wenn keiner gesetzt ist."""
    if not settings.db_key:
        return None
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALZ, info=_HKDF_ZWECK
    ).derive(settings.db_key.encode("utf-8"))


def ist_verschluesselt(roh: bytes) -> bool:
    """Trägt dieser Inhalt den Kopf einer Tresor-Datei?"""
    return roh[: len(MAGIE)] == MAGIE


def verschluesseln(roh: bytes) -> bytes:
    """Klartext → Tresor-Format. Ohne Schlüssel unverändert zurück."""
    key = _schluessel()
    if key is None:
        return roh
    nonce = os.urandom(NONCE_LAENGE)
    return MAGIE + nonce + AESGCM(key).encrypt(nonce, roh, None)


def entschluesseln(roh: bytes) -> bytes:
    """Tresor-Format → Klartext. Was keinen Kopf trägt, ist schon Klartext."""
    if not ist_verschluesselt(roh):
        return roh
    key = _schluessel()
    if key is None:
        raise TresorFehler(
            "Diese Datei ist verschlüsselt, aber es ist kein MONETEN_DB_KEY gesetzt."
        )
    nonce = roh[len(MAGIE) : len(MAGIE) + NONCE_LAENGE]
    try:
        return AESGCM(key).decrypt(nonce, roh[len(MAGIE) + NONCE_LAENGE :], None)
    except Exception as exc:  # InvalidTag und alles, was das Paket sonst wirft
        raise TresorFehler(
            "Die Datei liess sich nicht öffnen — falscher Schlüssel oder verändert."
        ) from exc


def schreiben(ziel: Path, roh: bytes) -> Path:
    """Legt ``roh`` ab — verschlüsselt, wenn ein Schlüssel da ist.

    Mit Schlüssel bekommt die Datei die Endung ``.enc`` zusätzlich, damit im
    Ordner auf einen Blick sichtbar ist, was verschlüsselt ist und was noch
    nicht. Zurückgegeben wird der **tatsächliche** Pfad.

    Geschrieben wird über eine Nebendatei und erst dann umbenannt: bricht der
    Vorgang ab, liegt keine halbe Datei da, die beim Lesen als beschädigt gilt.
    """
    ziel = ziel if _schluessel() is None else ziel.with_name(ziel.name + ".enc")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    neben = ziel.with_name(ziel.name + ".teil")
    with open(neben, "wb") as f:
        f.write(verschluesseln(roh))
        f.flush()
        os.fsync(f.fileno())
    # Nur der Dienst selbst. Ohne das entscheidet die umask des Containers —
    # in der Praxis 0644, also lesbar fuer jeden weiteren Benutzer im selben
    # Abbild. Unter Windows (Entwicklung) ist das folgenlos.
    os.chmod(neben, 0o600)
    os.replace(neben, ziel)
    return ziel


def lesen(pfad: Path) -> bytes:
    """Gibt den Klartext zurück — gleich, in welchem der beiden Formate er liegt."""
    return entschluesseln(Path(pfad).read_bytes())


def foto_ordner() -> Path:
    """Der EINZIGE Ort, an dem behaltene Beleg-Fotos liegen dürfen."""
    return Path(settings.attachments_dir) / "receipt_photos"


def entfernen(pfad: str | Path | None) -> bool:
    """Löscht ein Beleg-Foto — aber nur, wenn es wirklich im Foto-Ordner liegt.

    **Warum die Prüfung hier steht.** Der Pfad kommt aus der Datenbank, und die
    hat ihn irgendwann aus einem Formularfeld bekommen. Eine Löschfunktion, die
    jeden Pfad annimmt, ist ein Werkzeug zum Löschen beliebiger Dateien —
    einmal falsch gefüllt, und sie räumt ausserhalb auf.

    **Warum überhaupt gelöscht wird.** Frueher blieb das Foto liegen,
    wenn der vorgemerkte Beleg seiner Buchung zugeordnet und der Datensatz
    gelöscht wurde: der Ordner wuchs mit Bildern, auf die nichts mehr zeigte,
    und niemand hätte sie je wieder gefunden. Daten ohne Zweck sind kein
    neutraler Rest — sie sind das, was bei einem Einbruch mitgeht.

    Gibt zurück, ob wirklich etwas gelöscht wurde.
    """
    if not pfad:
        return False
    try:
        ziel = Path(pfad).resolve()
        ziel.relative_to(foto_ordner().resolve())
    except (ValueError, OSError):
        return False
    try:
        ziel.unlink()
    except OSError:
        return False
    return True
