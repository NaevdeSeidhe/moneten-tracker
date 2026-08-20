"""FastAPI-Einstiegspunkt des Moneten-Trackers.

Hier wird die App zusammengebaut:

1. Logging konfigurieren.
2. Static-Files (CSS, JS, Fonts) mounten.
3. Alle Router registrieren (Auth, Dashboard, Settings, Placeholder, WebAuthn).
4. Beim Start sicherstellen, dass das Attachments-Verzeichnis existiert
   und die Seeds (User, Konten, Kategorien) gelaufen sind.
5. Exception-Handler für 401, damit Browser auf ``/login`` umgeleitet werden.
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from moneten import __version__
from moneten.auth.pin import refresh_session
from moneten.auth.webauthn import router as webauthn_router
from moneten.config import settings
from moneten.db.seeds import seed_all
from moneten.db.session import SessionLocal, verschluesselung_pruefen
from moneten.routers import (
    accounts,
    auth_pin,
    budget,
    categories,
    compare,
    dashboard,
    forecast,
    import_bank,
    metrics,
    prices,
    quick,
    rules,
    savings_goals,
    settings_view,
    subscriptions,
    tax,
    transactions,
)
from moneten.templating import templates

# Korrekter MIME-Type fürs PWA-Manifest (StaticFiles würde sonst octet-stream
# liefern, was manche Browser ablehnen). Wird beim Laden des Moduls registriert,
# also lange bevor der erste Request kommt.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-7s  %(name)s :: %(message)s",
)
logger = logging.getLogger("moneten")


class _OhneGesundeHealthchecks(logging.Filter):
    """Lässt erfolgreiche ``/health``-Abrufe aus dem Zugriffsprotokoll weg.

    **Warum.** Der Docker-Healthcheck fragt alle 30 Sekunden — 2'880 Zeilen am
    Tag, die alle dasselbe sagen. Beim Suchen eines Deploy-Fehlers zeigte
    ``docker logs --tail 40`` daraufhin AUSSCHLIESSLICH Healthcheck-Zeilen. Ein
    Protokoll, in dem nichts zu finden ist, liest niemand mehr — und genau dann
    steht dort das, was man gebraucht hätte.

    **Nur die gesunden.** Antwortet ``/health`` einmal nicht mit 200, steht die
    Zeile drin. Der Filter nimmt Rauschen weg, keine Nachricht.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not ("/health" in text and " 200 " in text)


class _OhneSuchbegriffe(logging.Filter):
    """Schneidet die Fragezeichen-Anhänge aus dem Zugriffsprotokoll.

    Das Protokoll notiert sonst den vollen Pfad samt Suchbegriff. Die Datenbank
    ist verschlüsselt, eine Protokolldatei nicht — und sie wandert in jede
    Sicherung. Pfad, Methode, Status und Zeit bleiben stehen; weg ist nur der
    Inhalt der Suche.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            pfad, frage, _ = args[2].partition("?")
            if frage:
                record.args = (*args[:2], pfad + "?...", *args[3:])
        return True


for _filter in (_OhneGesundeHealthchecks(), _OhneSuchbegriffe()):
    logging.getLogger("uvicorn.access").addFilter(_filter)


# ---------------------------------------------------------------------------
# Lifespan-Hook: Verzeichnisse + Seeds vor dem ersten Request
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Wird beim Start und beim Shutdown ausgeführt."""
    attach_dir = Path(settings.attachments_dir)
    attach_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Moneten-Tracker v%s startet — Daten-Verzeichnis: %s", __version__, attach_dir.parent)
    logger.info(
        "DB-Verschlüsselung: %s",
        "aktiv (SQLCipher)" if settings.db_key else "inaktiv (Klartext — ok für lokale Tests)",
    )
    # Nicht nur sagen, dass verschlüsselt wird — nachsehen. Bricht ab, wenn die
    # Datei trotz gesetztem Schlüssel offen auf der Platte liegt.
    verschluesselung_pruefen()

    if not settings.dev_mode and not settings.db_key:
        logger.warning(
            "Produktionsmodus ohne MONETEN_DB_KEY: DB liegt im Klartext. "
            "Auf dem NAS MONETEN_DB_KEY setzen und/oder verschlüsselten Ordner nutzen."
        )

    with SessionLocal() as db:
        seed_all(db)

    yield

    logger.info("Moneten-Tracker beendet sauber.")


# ---------------------------------------------------------------------------
# App-Instanz
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Moneten-Tracker",
    version=__version__,
    description="Selbstgehostete Budget-App.",
    root_path=settings.root_path,
    lifespan=lifespan,
    # Härtung: kein offenes Swagger/ReDoc/OpenAPI. Die App ist eine HTML-über-HTMX-
    # App ohne öffentliches JSON-API; /docs, /redoc und /openapi.json lägen sonst
    # (ohne Login) im Tailscale-Netz offen. Zum Debuggen bei Bedarf wieder setzen.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Antworten komprimieren (HTML/CSS/JS): deutlich weniger Daten über die Leitung —
# spürbar schneller über Tailscale. Rein lokal, keine externe Abhängigkeit.
app.add_middleware(GZipMiddleware, minimum_size=500)

# Static-Files: CSS, JS, Fonts werden unter /static/ ausgeliefert. MIT Cache-Control,
# damit der Browser sie nicht bei JEDER Navigation neu lädt/revalidiert — spürbar
# langsam über Tailscale. Sicher, weil alle Assets per ?v=<version> cache-gebustet
# sind (ein Versionsbump erzwingt frisches Laden).
_static_dir = Path(__file__).parent / "static"


class _CachedStaticFiles(StaticFiles):
    """StaticFiles, das auf jede Antwort ein passendes ``Cache-Control`` setzt.

    Produktiv 30 Tage (sicher dank ``?v=<version>``). Im ``dev_mode`` dagegen
    ``no-store``: lokal ändert sich CSS/JS ständig OHNE Versionsbump, und der
    30-Tage-Cache liefert dann hartnäckig den alten Stand aus — beim Nachmessen
    im Browser führt das zu falschen Schlüssen über die eigene Änderung.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = (
            "no-store" if settings.dev_mode else "public, max-age=2592000"
        )
        return response


app.mount("/static", _CachedStaticFiles(directory=str(_static_dir)), name="static")


# ---------------------------------------------------------------------------
# Sicherheits-Header — strenge, offline-kompatible Content-Security-Policy.
# Die App lädt KEINE externen Ressourcen (Fonts/HTMX/Charts sind lokal), daher
# kann die Policy hart sein. 'unsafe-inline' nur für style-Attribute nötig
# (Inline-style="" in Templates); Scripts laufen ausschliesslich aus /static.
# ---------------------------------------------------------------------------
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    # 'none' statt 'self': die App setzt nirgends ein <base>-Element. Wer eines
    # unterschieben koennte, biegt damit JEDEN relativen Verweis der Seite auf
    # einen fremden Ort um — Formularziele eingeschlossen. Was man nicht
    # braucht, verbietet man.
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def sliding_session(request: Request, call_next):
    """Verlängert die Sitzung bei jeder Nutzung (gleitendes Zeitfenster).

    Ausgenommen sind statische Dateien — die holt der Browser im Hintergrund
    nach, teils aus dem Cache. Würde ein Icon-Nachladen die Sitzung verlängern,
    wäre die Leerlauf-Frist keine.

    Ebenso ausgenommen ist ``/logout``: dort löscht die Route das Cookie, und
    diese Middleware würde es unmittelbar danach wieder setzen.
    """
    response = await call_next(request)
    pfad = request.url.path
    if not pfad.startswith("/static") and pfad != "/logout" and response.status_code < 400:
        refresh_session(request, response)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Setzt defensive HTTP-Header auf jeder Antwort."""
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    # **Nichts ausser den statischen Dateien darf zwischengespeichert werden.**
    #
    # Ohne Anweisung entscheidet der Browser selbst — und er entscheidet oft für
    # „behalten". Die Folge sah man am „Zurück" nach dem Abmelden: die
    # Übersicht mit allen Zahlen stand wieder da, ohne Sitzung, aus dem
    # Zwischenspeicher. Dasselbe gilt für ausgelieferte Quittungen: die Datei
    # blieb im Cache-Ordner des Geräts liegen, unabhängig von der App.
    #
    # ``no-store`` und nicht ``no-cache``: ``no-cache`` erlaubt das Ablegen und
    # verlangt nur eine Rückfrage. Genau das Ablegen ist hier das Problem.
    #
    # ``setdefault``: die statischen Dateien setzen ihre eigene, lange
    # Gültigkeit (siehe ``_CachedStaticFiles``) und behalten sie.
    if not request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-store")

    # **HSTS — nur ausserhalb des Entwicklungsmodus.**
    #
    # Gemessen: beide Zugangswege lieferten CSP, X-Frame-Options,
    # nosniff, Referrer- und Permissions-Policy, aber keinen HSTS-Header. Ohne
    # ihn genügt ein einziger Aufruf über ``http://``, um die Sitzung im Klartext
    # zu zeigen — der Browser hat nichts, das ihn vorher auf https zwingt.
    #
    # **Warum das gefahrlos ist, obwohl der LAN-Weg ein eigenes Zertifikat
    # nutzt:** HSTS gilt laut RFC 6797 nur für Hostnamen, nicht für
    # IP-Adressen. Über ``https://<ip>:8443`` ist der Header damit wirkungslos —
    # die Zertifikatswarnung dort bleibt wie bisher wegklickbar. Wirksam wird er
    # auf dem Namensweg (Tailscale, gültiges Zertifikat), und genau dort soll er
    # wirken.
    #
    # Im ``dev_mode`` läuft die App bewusst über http; ein HSTS-Header würde den
    # Browser dann für Monate auf https festnageln — auf localhost eine Sackgasse.
    if not settings.dev_mode:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=15552000; includeSubDomains"
        )
    return response


# ---------------------------------------------------------------------------
# Router-Registrierung
# ---------------------------------------------------------------------------

# Reihenfolge ist Geschmackssache; die /-Route muss aber als letzte hinzugefügt
# werden, damit die spezifischeren Pfade nicht überschattet werden.
app.include_router(auth_pin.router)
app.include_router(webauthn_router)
app.include_router(settings_view.router)

app.include_router(transactions.router, prefix="/transactions")
app.include_router(accounts.router, prefix="/accounts")
app.include_router(budget.router, prefix="/budget")
app.include_router(subscriptions.router, prefix="/subscriptions")
app.include_router(savings_goals.router, prefix="/savings-goals")
app.include_router(tax.router, prefix="/steuern")
app.include_router(prices.router, prefix="/preise")
app.include_router(metrics.router, prefix="/verlaeufe")
app.include_router(import_bank.router, prefix="/import")
app.include_router(rules.router, prefix="/rules")
app.include_router(categories.router, prefix="/categories")
app.include_router(forecast.router, prefix="/forecast")
app.include_router(compare.router, prefix="/compare")
app.include_router(quick.router, prefix="/quick")

app.include_router(dashboard.router)


# ---------------------------------------------------------------------------
# Health-Check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Wird vom Docker-Healthcheck genutzt."""
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# Exception-Handler — 401 macht Server-Side-Redirect für normale Browser
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Weiterleiten statt Fehlerseite, wenn die Ausnahme ein Ziel mitbringt.

    HTMX-Requests werden über den ``HX-Redirect``-Header (von der Dependency
    ``require_login`` gesetzt) bereits korrekt umgeleitet. Für klassische
    Aufrufe folgen wir dem ``Location``-Header der Exception.

    Das gilt für 401 (nicht angemeldet → ``/login``) und für 403 aus derselben
    Dependency (es gilt noch die Start-PIN → Wechsel-Seite). Ohne den zweiten
    Fall wäre die Sperre eine Sackgasse: man sähe eine Fehlerseite und käme
    nirgends hin.
    """
    ziel = (exc.headers or {}).get("Location")
    if exc.status_code in (401, 403) and ziel:
        if request.headers.get("HX-Request") == "true":
            # HTMX folgt dem Header — der Rumpf darf leer bleiben.
            return HTMLResponse(status_code=exc.status_code, headers=exc.headers or {})
        return RedirectResponse(url=ziel, status_code=303)

    # Generic fallback: schicke eine kleine Error-Seite.
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Validierungs-Fehler hübsch rendern.

    Bewusst **keine** internen Details (Feldnamen/Typen) ausgeben — der
    rohe Exception-Text gehört nicht in die Antwort (Info-Disclosure).
    """
    logger.info("Validierungsfehler bei %s: %s", request.url.path, exc)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": 422, "detail": "Die Anfrage war ungültig oder unvollständig."},
        status_code=422,
    )
