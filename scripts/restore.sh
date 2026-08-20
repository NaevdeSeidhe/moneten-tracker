#!/usr/bin/env bash
# =========================================================================
# Moneten-Tracker — Wiederherstellung aus einem Backup.
#
# Spielt einen DB-Snapshot (von backup.sh) zurück und optional die Attachments.
# Der Container wird dafür kurz gestoppt, damit keine Schreibzugriffe kollidieren.
#
# Aufruf:
#   ./restore.sh /pfad/zu/backups/db/2026-05-31.db [ATTACHMENTS-ORDNER]
#
# Sicherheitsnetz: die aktuelle DB wird vorher nach *.pre-restore weggesichert.
#
# WELCHE Datei zurueckgespielt wird, stand hier einmal fest: `moneten.db`. Das
# war ein Fehler mit Ansage — eine Anlage aus der Zeit vor der Umbenennung liest
# `bilanz.db`, und dann meldete dieses Skript "OK.", ohne die Live-Datenbank
# anzufassen: das Backup landete daneben, und die Sicherung der alten Datei
# entstand nie, weil ihre Bedingung dieselbe falsche Datei prueft. Der Name
# kommt jetzt aus MONETEN_DATABASE_URL, also aus derselben Quelle wie bei der
# App, und bei Unklarheit bricht das Skript ab statt zu raten.
# =========================================================================
set -euo pipefail

DB_BACKUP="${1:?Pfad zum DB-Backup angeben, z.B. /pfad/zu/backups/db/2026-05-31.db}"
ATTACH_BACKUP="${2:-}"
CONTAINER="${MONETEN_CONTAINER:-moneten}"
HOST_DATA="${MONETEN_HOST_DATA:-/volume1/moneten/data}"

# --- Welche Datei liest die App? -----------------------------------------
DB_NAME="${MONETEN_DB_NAME:-}"
if [ -z "${DB_NAME}" ] && [ -n "${MONETEN_DATABASE_URL:-}" ]; then
    # sqlite:///./data/x.db  |  sqlite:////app/data/x.db  -> x.db
    case "${MONETEN_DATABASE_URL}" in
        sqlite*) DB_NAME="$(basename "${MONETEN_DATABASE_URL%%\?*}")" ;;
    esac
fi
if [ -z "${DB_NAME}" ]; then
    # Keine Konfiguration erreichbar (der Aufruf laeuft auf dem HOST, die .env
    # liegt im Container): dann aus dem Datenordner ableiten.
    VORHANDEN="$(find "${HOST_DATA}" -maxdepth 1 -name '*.db' -printf '%f\n' 2>/dev/null | sort)"
    ANZAHL="$(printf '%s' "${VORHANDEN}" | grep -c . || true)"
    if [ "${ANZAHL}" -eq 1 ]; then
        DB_NAME="${VORHANDEN}"
    elif [ "${ANZAHL}" -gt 1 ]; then
        echo "[restore] ABBRUCH: mehrere Datenbanken in ${HOST_DATA}:" >&2
        printf '           %s\n' ${VORHANDEN} >&2
        echo "           Bitte die richtige benennen:  MONETEN_DB_NAME=<datei.db> $0 ..." >&2
        exit 1
    else
        DB_NAME="moneten.db"
    fi
fi
DB_LIVE="${HOST_DATA}/${DB_NAME}"
echo "[restore] Ziel: ${DB_LIVE}"

if [ ! -f "${DB_BACKUP}" ]; then
    echo "[restore] FEHLER: Backup-Datei nicht gefunden: ${DB_BACKUP}" >&2
    exit 1
fi

echo "[restore] Container stoppen ..."
docker stop "${CONTAINER}" || true

# **Nachsehen, ob er wirklich steht.** ``|| true`` oben schluckt JEDEN Fehler —
# auch den Fall, dass der Container anders heisst als erwartet. Dann laeuft die
# App weiter, und die naechste Zeile kopiert eine Sicherung ueber eine Datei,
# in die gerade geschrieben wird: die Live-Daten sind weg, und die
# wiederhergestellte Datenbank ist beschaedigt.
#
# Genau diese Annahme war beim Dateinamen schon einmal falsch (siehe Kopf
# dieser Datei). Beim Containernamen kostet sie mehr.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "${CONTAINER}"; then
    echo "[restore] FEHLER: Der Container '${CONTAINER}' laeuft noch." >&2
    echo "[restore] Ein Einspielen wuerde in eine offene Datenbank schreiben." >&2
    echo "[restore] Richtigen Namen setzen:  MONETEN_CONTAINER=<name> $0 ..." >&2
    echo "[restore] Laufende Container:" >&2
    docker ps --format '  - {{.Names}}' >&2 || true
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -f "${DB_LIVE}" ]; then
    echo "[restore] Aktuelle DB sichern → ${DB_NAME}.pre-restore-${STAMP}"
    cp "${DB_LIVE}" "${DB_LIVE}.pre-restore-${STAMP}"
fi

echo "[restore] DB einspielen ..."
cp "${DB_BACKUP}" "${DB_LIVE}"
# WAL/SHM einer evtl. alten DB entfernen — der Snapshot ist self-contained.
rm -f "${DB_LIVE}-wal" "${DB_LIVE}-shm"

if [ -n "${ATTACH_BACKUP}" ] && [ -d "${ATTACH_BACKUP}" ]; then
    echo "[restore] Attachments einspielen ..."
    mkdir -p "${HOST_DATA}/attachments"
    rsync -a --delete "${ATTACH_BACKUP}/" "${HOST_DATA}/attachments/"
fi

echo "[restore] Container starten ..."
docker start "${CONTAINER}"

echo "[restore] OK. Falls etwas nicht stimmt, liegt die vorherige DB unter"
echo "          ${DB_LIVE}.pre-restore-${STAMP}"
