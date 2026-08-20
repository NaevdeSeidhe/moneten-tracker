# =========================================================================
# Moneten-Tracker — Produktions-Image
# Python 3.12-slim, schlankes Single-Stage-Build.
# Build:   docker build -t moneten:latest .
# Run:     docker compose up -d
# =========================================================================
FROM python:3.12-slim AS base

# Verhindert .pyc-Dateien und sorgt für unbuffered Logs (wichtig für Docker).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System-Pakete:
#   tini             — sauberes Init/SIGTERM-Handling
#   curl             — Healthchecks
#   ca-certificates  — CA-Bundle für den Wheel-Abruf von PyPI. NUR beim Bauen:
#                      zur Laufzeit ruft die App nichts nach draussen auf, Schriften
#                      und HTMX liegen als Datei unter src/moneten/static/.
#   tesseract-ocr(+deu) — OCR-Fallback für nicht durchsuchbare Quittungen
#   libgl1, libglib2.0-0 — Laufzeit-Libs für opencv (Abhängigkeit von RapidOCR, der
#                          primären OCR-Engine); sonst scheitert `import cv2` (libGL.so.1)
#   util-linux       — liefert `setpriv`. Damit gibt der Entrypoint die
#                      Privilegien ab. Das Paket ist in Debian zwar als
#                      „required" eingestuft und praktisch immer da — aber
#                      „praktisch immer" ist keine Zusicherung, und wenn es
#                      fehlte, hinge daran der Start des Containers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini curl ca-certificates tesseract-ocr tesseract-ocr-deu \
      libgl1 libglib2.0-0 util-linux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Abhängigkeiten: feste Fassungen, mit Prüfsummen ---------------------
#
# ``requirements-docker.txt`` nagelt jede Fassung fest. Vorher löste
# ``pip install -e .`` bei JEDEM Bau frisch auf — zwischen zwei Bauten desselben
# Quelltextes konnten andere Pakete landen, ohne dass irgendwo etwas stand.
# ``--require-hashes`` prüft zusätzlich, dass geliefert wird, was gemeint ist.
#
# Diese Schicht hängt NUR an dieser einen Datei: solange sich die Fassungen nicht
# ändern, überlebt sie jede Code-Änderung. Vorher stand ``COPY src/`` davor, und
# damit war der Layer-Cache bei jeder Zeile Code hin — der Kommentar an dieser
# Stelle versprach genau das Gegenteil.
COPY requirements-docker.txt ./
# **Kein ``pip install --upgrade pip`` mehr.** Es holte die neueste pip-Fassung
# von PyPI — ohne Fassungsangabe und ohne Pruefsumme —, und ausgerechnet dieses
# Werkzeug prueft danach die Pruefsummen aller anderen Pakete. Wer eine
# boesartige pip-Fassung unterbringt, fuehrt beim naechsten Bau Code im
# Bau-Container aus und kann das ``--require-hashes`` der naechsten Zeile
# wirkungslos machen; das Abbild ginge trotzdem als geprueft nach ghcr.io.
#
# Benutzt wird jetzt das pip, das im Basis-Abbild steckt: dessen Fassung haengt
# an der Fassungsmarke des Basis-Abbilds und aendert sich nicht zwischen zwei
# Bauten desselben Quelltextes.
RUN pip install --require-hashes -r requirements-docker.txt

# --- Die App selbst ------------------------------------------------------
#
# ``--no-deps``: die Abhängigkeiten stehen bereits oben, und zwar in den festen
# Fassungen. Ohne das Flag löste pip sie hier erneut auf und könnte sie
# überschreiben. README.md muss mit, weil pyproject.toml sie als 'readme'
# referenziert (hatchling prüft die Existenz beim Build).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-deps -e .

# Alembic-Migrationen und Container-Skripte.
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/

# Datenverzeichnis als Volume — wird im Compose-File gemountet.
RUN mkdir -p /app/data/attachments

# Unprivilegierter Benutzer für den Serverbetrieb.
#
# Bewusst OHNE ``USER``-Zeile: der Entrypoint MUSS als root anfangen. Der
# Datenordner kommt als Bind-Mount vom Host und gehört dort root — alles, was
# frühere Fassungen dieses Images angelegt haben, tut es auch. Ein Container,
# der direkt unprivilegiert startet, scheitert an der ersten Migration, und
# ``restart: unless-stopped`` macht daraus eine Neustartschleife: die App ist
# weg, bis jemand per SSH ein ``chown`` nachreicht.
#
# Der Entrypoint richtet die Rechte deshalb selbst und gibt die Privilegien
# danach ab (siehe ``scripts/entrypoint.sh``). Notausgang: ``MONETEN_UID=0``.
RUN useradd --system --uid 10001 --user-group --create-home \
      --home-dir /home/moneten --shell /usr/sbin/nologin moneten \
 && chown -R moneten:moneten /app

EXPOSE 8000

# Healthcheck nutzt den /health-Endpoint der FastAPI-App.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# tini → bash → entrypoint.sh kümmert sich um Migrationen + Server-Start.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash", "/app/scripts/entrypoint.sh"]
