#!/usr/bin/env bash
# =========================================================================
# Moneten-Tracker — tägliches Backup (auf dem NAS via Aufgabenplaner/cron)
#
# Erstellt einen KONSISTENTEN Snapshot der DB, auch bei laufendem Container
# und auch wenn die DB mit SQLCipher VERSCHLÜSSELT ist:
#   * VACUUM INTO läuft über die App-Engine (kennt MONETEN_DB_KEY) und schreibt
#     eine in sich geschlossene Kopie (keine WAL/SHM-Dateien nötig).
#   * Ist ein Key gesetzt, ist die Kopie ebenfalls verschlüsselt — sonst Klartext.
# Zusätzlich werden die Quittungs-Anhänge gesichert. Alte Backups (>90 Tage)
# werden aufgeräumt.
#
# Aufruf:   ./backup.sh [ZIEL-BASIS]      (Default: /volume1/backups/moneten)
#
# Off-Site (optional, standardmäßig AUS): siehe Abschnitt „Off-Site-Push" unten.
# =========================================================================
set -euo pipefail

TARGET_BASE="${1:-/volume1/backups/moneten}"
CONTAINER="${MONETEN_CONTAINER:-moneten}"
# Pfad der Attachments AUF DEM HOST (Bind-Mount). Bei Bedarf anpassen.
HOST_DATA="${MONETEN_HOST_DATA:-/volume1/moneten/data}"

DATE="$(date +%Y-%m-%d)"
DB_DIR="${TARGET_BASE}/db"
ATTACH_DIR="${TARGET_BASE}/attachments"
mkdir -p "${DB_DIR}" "${ATTACH_DIR}"

# Rest eines früher abgebrochenen Laufs entfernen — sonst scheitert VACUUM INTO mit
# „output file already exists". Die Datei liegt dank Bind-Mount auch direkt am Host-Pfad.
rm -f "${HOST_DATA}/.snapshot.db" 2>/dev/null || true
docker exec "${CONTAINER}" rm -f /app/data/.snapshot.db 2>/dev/null || true

# Der Import kennt BEIDE Paketnamen. Dieses Skript wird aus dem NEUEN Quellbaum
# hochgeladen, laeuft beim Deploy aber im ALTEN, noch laufenden Container — und
# dort heisst das Paket vor der Umbenennung ``bilanz``. Ohne den Rueckfall
# scheitert genau das Backup, das vor den Migrationen das Sicherheitsnetz ist:
#   ModuleNotFoundError: No module named 'moneten'
# Der Zweig darf weg, sobald kein Container von vor der Umbenennung mehr laufen
# kann — er kostet nichts und rettet den einen Deploy, der ueber die Grenze geht.
echo "[backup] Konsistenten DB-Snapshot erstellen (VACUUM INTO, key-aware) ..."
docker exec "${CONTAINER}" python -c "
try:
    from moneten.db.session import engine
except ModuleNotFoundError:
    from bilanz.db.session import engine
with engine.connect() as conn:
    conn.exec_driver_sql(\"VACUUM INTO '/app/data/.snapshot.db'\")
print('snapshot ok')
"
# Der Snapshot liegt dank Bind-Mount bereits auf dem Host — wir verschieben ihn direkt
# (Rename auf demselben /volume1 → sofort, kein Extra-Platz). Das umgeht das auf Synology
# gelegentlich störende „docker cp"; nur falls die Datei wider Erwarten nicht am Host-Pfad
# liegt, wird auf docker cp zurückgefallen.
if [ -f "${HOST_DATA}/.snapshot.db" ]; then
    mv -f "${HOST_DATA}/.snapshot.db" "${DB_DIR}/${DATE}.db"
else
    docker cp "${CONTAINER}:/app/data/.snapshot.db" "${DB_DIR}/${DATE}.db"
    docker exec "${CONTAINER}" rm -f /app/data/.snapshot.db
fi

# ---------------------------------------------------------------------------
# Nachsehen, ob der Schnappschuss wirklich verschluesselt ist.
#
# **Warum das hier steht und nicht in einem Test.** Ob ``VACUUM INTO`` aus einer
# SQLCipher-Verbindung eine verschluesselte oder eine offene Datei schreibt,
# laesst sich nur dort messen, wo SQLCipher wirklich laeuft — also hier, auf dem
# NAS, mit dem echten Schluessel. Auf dem Entwicklungsrechner gibt es kein Rad
# fuer sqlcipher3, die Frage bleibt dort unbeantwortbar.
#
# Eine unverschluesselte Sicherung waere der stillste denkbare Fehler: die
# Datenbank liegt geschuetzt, und daneben liegt jede Nacht eine offene Kopie
# davon — mit jeder Buchung, jedem Betrag, jedem Haendler. Niemand merkt es,
# denn die Sicherung funktioniert ja.
#
# Der Test ist billig: eine offene SQLite-Datei beginnt mit „SQLite format 3".
# Ein Geheimtext beginnt mit Zufall.
if docker exec "${CONTAINER}" sh -c 'test -n "$MONETEN_DB_KEY"' 2>/dev/null; then
    if head -c 15 "${DB_DIR}/${DATE}.db" 2>/dev/null | grep -q "SQLite format 3"; then
        echo "[backup] ABBRUCH: Der Schnappschuss ist UNVERSCHLUESSELT, obwohl ein"
        echo "[backup]          MONETEN_DB_KEY gesetzt ist. Die Datei wird geloescht,"
        echo "[backup]          damit keine offene Kopie der Datenbank liegenbleibt:"
        echo "[backup]          ${DB_DIR}/${DATE}.db"
        rm -f "${DB_DIR}/${DATE}.db"
        exit 1
    fi
    echo "[backup] Schnappschuss ist verschluesselt (kein SQLite-Kopf)."
fi

echo "[backup] Attachments sichern ..."
if [ -d "${HOST_DATA}/attachments" ]; then
    rsync -a --delete "${HOST_DATA}/attachments/" "${ATTACH_DIR}/"
else
    echo "[backup] (kein Attachments-Ordner unter ${HOST_DATA}/attachments — übersprungen)"
fi

# Aufräumen: DB-Backups älter als 90 Tage löschen.
find "${DB_DIR}" -name "*.db" -mtime +90 -delete 2>/dev/null || true

echo "[backup] OK — ${DB_DIR}/${DATE}.db"

# -------------------------------------------------------------------------
# Off-Site-Push (OPTIONAL, vorbereiteter Hook — standardmäßig deaktiviert).
# Aktivieren, indem du diese Env-Variablen setzt (z.B. im Cron-Job):
#   MONETEN_OFFSITE_RCLONE_REMOTE="b2:mein-bucket/moneten"   # rclone-Remote
#   MONETEN_BACKUP_GPG_RECIPIENT="deine@mail"                # optional: GPG-Verschlüsselung
# Voraussetzung: rclone (+ ggf. gpg) auf dem NAS installiert und konfiguriert.
# Beides muss VORHER eingerichtet sein: das rclone-Remote mit `rclone config`
# (der Name links vom Doppelpunkt), der GPG-Empfänger im Schlüsselbund des
# Benutzers, unter dem der Cron-Job läuft. Fehlt eines, bricht der Push ab —
# die lokale Sicherung ist zu diesem Zeitpunkt bereits geschrieben.
# -------------------------------------------------------------------------
if [ -n "${MONETEN_OFFSITE_RCLONE_REMOTE:-}" ]; then
    PUSH_FILE="${DB_DIR}/${DATE}.db"
    if [ -n "${MONETEN_BACKUP_GPG_RECIPIENT:-}" ]; then
        echo "[backup] Off-Site: GPG-verschlüsseln ..."
        gpg --yes --batch --encrypt --recipient "${MONETEN_BACKUP_GPG_RECIPIENT}" "${PUSH_FILE}"
        PUSH_FILE="${PUSH_FILE}.gpg"
    fi
    echo "[backup] Off-Site: rclone copy → ${MONETEN_OFFSITE_RCLONE_REMOTE}"
    rclone copy "${PUSH_FILE}" "${MONETEN_OFFSITE_RCLONE_REMOTE}/"

    # **Die Belege gehoeren ebenso verschluesselt.** Vorher verschluesselte GPG
    # nur die Datenbank, und die Quittungen wanderten Datei fuer Datei im
    # Klartext in die Cloud — Haendler, Betraege, Uhrzeiten, Medikamente,
    # teilweise die Kartennummer auf dem Bon. Wer die Datenbank fuer
    # schuetzenswert haelt, meint die Belege mit.
    if [ -n "${MONETEN_BACKUP_GPG_RECIPIENT:-}" ]; then
        if [ -d "${ATTACH_DIR}" ]; then
            echo "[backup] Off-Site: Belege packen und verschluesseln ..."
            ATTACH_TGZ="${DB_DIR}/${DATE}-attachments.tar.gz"
            tar -czf "${ATTACH_TGZ}" -C "$(dirname "${ATTACH_DIR}")" "$(basename "${ATTACH_DIR}")"
            gpg --yes --batch --encrypt --recipient "${MONETEN_BACKUP_GPG_RECIPIENT}" "${ATTACH_TGZ}"
            rm -f "${ATTACH_TGZ}"
            rclone copy "${ATTACH_TGZ}.gpg" "${MONETEN_OFFSITE_RCLONE_REMOTE}/"
            rm -f "${ATTACH_TGZ}.gpg"
        fi
    else
        # Ohne Empfaenger gibt es keine Verschluesselung — dann bleiben die
        # Belege hier. Lieber eine unvollstaendige Off-Site-Sicherung als eine,
        # die den Inhalt jeder Quittung offen in fremden Speicher legt.
        echo "[backup] Off-Site: Belege NICHT gesendet — kein GPG-Empfaenger gesetzt." >&2
        echo "[backup] Off-Site: MONETEN_BACKUP_GPG_RECIPIENT setzen, dann gehen sie verschluesselt mit." >&2
    fi
    echo "[backup] Off-Site OK"
else
    echo "[backup] Off-Site übersprungen (MONETEN_OFFSITE_RCLONE_REMOTE nicht gesetzt)."
fi
