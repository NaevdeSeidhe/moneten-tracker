#!/usr/bin/env bash
# =========================================================================
# Moneten-Tracker — Container-Entrypoint
#
# Zwei Aufgaben, in dieser Reihenfolge:
#   1. Privilegien abgeben — der Server läuft NICHT als root.
#   2. Ausstehende Datenbank-Migrationen anwenden, dann Uvicorn starten.
#
# **Warum das Abgeben hier passiert und nicht per ``USER`` im Dockerfile.**
# Der Datenordner ist ein Bind-Mount vom Host. Auf einer bestehenden Anlage
# gehört alles darin ``root`` — angelegt von genau diesem Container, als er noch
# als root lief. Ein Image mit ``USER`` scheitert dort an der ersten Migration
# (SQLite braucht Schreibrecht auf die Datei UND auf den Ordner, für -wal/-shm),
# ``set -e`` beendet das Skript, und ``restart: unless-stopped`` macht daraus
# eine Neustartschleife: die App ist weg, bis jemand per SSH ein ``chown``
# nachreicht. Also richtet dieses Skript die Rechte selbst — einmal, und nur
# wenn nötig — und gibt danach ab.
#
# Notausgang: ``MONETEN_UID=0`` lässt alles als root laufen wie bisher. Wer
# eigene Rechte braucht (etwa damit der SSH-Benutzer des NAS die Sicherungen
# lesen kann), setzt ``MONETEN_UID``/``MONETEN_GID`` auf seine eigenen Werte.
# =========================================================================
set -euo pipefail

: "${MONETEN_UID:=10001}"
: "${MONETEN_GID:=10001}"
DATEN="${MONETEN_DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ] && [ "$MONETEN_UID" != "0" ]; then
    mkdir -p "$DATEN"

    # Erst suchen, dann anfassen: ein ``chown -R`` über tausende Belege bei
    # JEDEM Start wäre Verschwendung. ``find … -quit`` hält beim ersten Treffer
    # an und kostet auf einer sauberen Anlage fast nichts.
    if [ -n "$(find "$DATEN" ! -uid "$MONETEN_UID" -print -quit 2>/dev/null)" ]; then
        echo "[moneten] Datenordner gehört noch jemand anderem — setze Besitzrechte auf ${MONETEN_UID}:${MONETEN_GID} ..."
        if ! chown -R "${MONETEN_UID}:${MONETEN_GID}" "$DATEN"; then
            # Klartext statt eines kryptischen SQLite-Fehlers drei Zeilen später.
            echo "[moneten] FEHLER: Die Besitzrechte von ${DATEN} lassen sich nicht ändern." >&2
            echo "[moneten] Das passiert bei Freigaben, die den Eigentümer erzwingen (SMB/CIFS/NFS)." >&2
            echo "[moneten] Entweder am Host setzen: chown -R ${MONETEN_UID}:${MONETEN_GID} <datenordner>" >&2
            echo "[moneten] oder MONETEN_UID=0 in der .env — dann läuft die App wie bisher als root." >&2
            exit 1
        fi
    fi

    # ``exec setpriv`` und nicht ``su``/``runuser``: ein echtes exec ohne
    # Zwischenprozess. Sonst hinge tini an einem Wrapper, SIGTERM käme beim
    # Server nicht an, und das Stoppen kostete jedes Mal die zehn Sekunden bis
    # zum SIGKILL. ``--clear-groups`` statt ``--init-groups``, damit auch ein
    # eigenes UID/GID-Paar funktioniert, für das es im Image keinen
    # Benutzereintrag gibt.
    # ``HOME`` mitgeben: ``setpriv`` lässt die Umgebung stehen, der abgegebene
    # Prozess erbte sonst ``/root`` und dürfte dort nichts schreiben. Heute
    # schreibt nichts nach HOME (die OCR-Modelle liegen im Paket) — aber wenn es
    # je passiert, soll es nicht an einer Zeile scheitern, die niemand sucht.
    # ``/tmp`` gilt für jede Kennung, auch für ein eigenes UID/GID-Paar.
    export HOME="${MONETEN_HOME:-/tmp}"

    # Fehlt ``setpriv``, läuft der Server als root weiter — mit Ansage.
    #
    # **Warum nicht abbrechen.** Ein Abbruch hier hiesse: Container endet,
    # ``restart: unless-stopped`` startet ihn neu, Neustartschleife, App weg.
    # Als root zu laufen ist der Zustand von vorher; er ist nicht schlimmer,
    # nur nicht besser. Eine Härtung darf nicht der Grund sein, warum eine
    # Finanz-App nicht mehr hochkommt.
    if ! command -v setpriv >/dev/null 2>&1; then
        echo "[moneten] WARNUNG: setpriv fehlt im Abbild — der Server läuft als" \
             "root weiter. Erwartet wird es aus dem Paket util-linux." >&2
    else
        # **Über ``bash`` neu starten, nicht die Datei direkt.**
        #
        # ``setpriv … "$0"`` würde das Skript als Programm ausführen — und dafür
        # bräuchte es das Ausführbar-Bit. Das Paket kommt aus einem Windows-tar
        # und trägt keines; das Docker-``CMD`` ruft die Datei deshalb ebenfalls
        # über ``bash`` auf. Gemessen auf dem NAS:
        #
        #     setpriv: failed to execute /app/scripts/entrypoint.sh: Permission denied
        #
        # Danach beendete sich der Container, ``restart: unless-stopped`` startete
        # ihn neu — eine Neustartschleife, genau die, die dieses Skript vermeiden
        # soll. Mit ``bash "$0"`` sind die Rechte-Bits der Datei gleichgültig.
        echo "[moneten] Privilegien abgeben — UID ${MONETEN_UID}, GID ${MONETEN_GID}"
        exec setpriv --reuid="$MONETEN_UID" --regid="$MONETEN_GID" --clear-groups \
             bash "$0" "$@"
    fi
fi

echo "[moneten] Läuft als UID $(id -u). Migrationen prüfen / anwenden ..."
alembic upgrade head

echo "[moneten] Server starten auf 0.0.0.0:8000 ..."

# ``--forwarded-allow-ips`` entscheidet, WEM die App die Header
# ``X-Forwarded-For`` und ``X-Forwarded-Proto`` glaubt. Daran hängen zwei Dinge:
# die Absenderadresse, unter der die Login-Drossel zählt, und das Schema, aus dem
# die WebAuthn-Herkunft gebaut wird.
#
# Vorgabe ``*`` = wie bisher, also jedem. Bewusst so geblieben, und die Härtung
# liegt anderswo: Docker maskiert jede Verbindung hinter dem Brücken-Gateway,
# die App sieht deshalb bei allen Anfragen dieselbe Adresse. Ein Wert, der den
# Reverse-Proxy einschliesst, schliesst damit auch jedes andere Gerät ein — er
# schliesst die Lücke also nicht. Das tut die Port-Bindung auf ``127.0.0.1`` in
# ``docker-compose.yml``: dann kommt ausser dem Host niemand herein.
#
# Ein falsch gesetzter Wert ist dagegen teuer — das Schema fällt auf http
# zurück, und angelegte Passkeys passen nicht mehr. Erklärung in ``.env.example``.
# **MONETEN_PROXY_HOPS=0 heisst: kein Proxy davor — dann darf uvicorn die
# Absenderadresse auch nicht aus X-Forwarded-For nehmen.**
#
# Vorher stand hier unbedingt ``--proxy-headers --forwarded-allow-ips="*"``.
# Damit ersetzt uvicorn ``request.client.host`` durch den ERSTEN Eintrag der
# Kopfzeile — und den setzt der Klopfende selbst. Die Drossel sieht bei
# ``hops=0`` bewusst nicht in die Kopfzeile, bekam aber trotzdem einen vom
# Klopfenden gewaehlten Wert: pro erfundener Adresse ein eigener Zaehler.
# Die Zusage „mit 0 wird die Kopfzeile gar nicht angesehen" stimmte also nur
# im Programm, nicht in der Anlage darum herum.
#
# Ohne Proxy ist die einzige ehrliche Quelle die Adresse der Verbindung selbst.
if [ "${MONETEN_PROXY_HOPS:-1}" = "0" ]; then
    echo "[moneten] MONETEN_PROXY_HOPS=0 — X-Forwarded-For wird nicht ausgewertet."
    exec uvicorn moneten.main:app --host 0.0.0.0 --port 8000
fi

exec uvicorn moneten.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips="${MONETEN_FORWARDED_ALLOW_IPS:-*}"
