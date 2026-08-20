"""SQLAlchemy-Engine, SessionFactory und FastAPI-Dependency.

Wir nutzen die moderne SQLAlchemy-2.0-API mit ``DeclarativeBase``.
SQLite bekommt eine kleine Sonderbehandlung (``check_same_thread=False``
sowie WAL-Modus für bessere Schreib-Parallelität).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from moneten.config import settings


class Base(DeclarativeBase):
    """Gemeinsame Deklarations-Basis aller ORM-Models.

    Alle konkreten Tabellen-Klassen erben hiervon. Alembic erkennt das
    Schema automatisch über ``Base.metadata``.
    """


def _build_engine():
    """Erzeugt die SQLAlchemy-Engine passend zur DB-URL.

    Drei Fälle:
    1. **SQLite + ``MONETEN_DB_KEY`` gesetzt** → verschlüsselte Datei via SQLCipher.
       Ein eigener Connection-Creator setzt den Schlüssel als allererstes PRAGMA.
       Das Paket ``sqlcipher3`` wird nur dann importiert (im Docker-Image vorhanden;
       lokal/Tests ohne Schlüssel wird dieser Zweig nie betreten).
    2. **SQLite ohne Schlüssel** → Klartext-Datei (Entwicklung/Tests).
    3. Andere DBs → Standard-Engine.

    Für SQLite immer: ``check_same_thread=False`` (Uvicorn-Worker reichen die
    Connection weiter), WAL-Journal + Foreign-Keys via ``PRAGMA``.
    """
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")

    if is_sqlite and settings.db_key:
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import QueuePool
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore[import-not-found]

        db_path = make_url(url).database or ":memory:"
        key = settings.db_key.replace("'", "''")  # Quote-Escape für das PRAGMA

        def _creator():
            conn = sqlcipher.connect(db_path, check_same_thread=False)
            conn.execute(f"PRAGMA key = '{key}'")  # MUSS vor allem anderen kommen
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn

        # WICHTIG: poolclass explizit setzen. Die URL "sqlite://" sieht für SQLAlchemy wie
        # eine In-Memory-DB aus → Default wäre SingletonThreadPool (EINE geteilte Verbindung).
        # Die Handler laufen im Threadpool (sync def) — eine geteilte Verbindung über Threads
        # ist nicht thread-sicher. QueuePool gibt jedem Thread seine eigene Verbindung.
        return create_engine(
            "sqlite://", module=sqlcipher, creator=_creator,
            poolclass=QueuePool, pool_size=5, max_overflow=5, future=True,
        )

    connect_args: dict = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args, future=True)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI-Dependency: liefert eine Session und schliesst sie nach dem Request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Nachsehen, ob die Verschlüsselung wirklich greift
# ---------------------------------------------------------------------------

#: Womit jede unverschlüsselte SQLite-Datei anfängt.
_SQLITE_KOPF = b"SQLite format 3\x00"

#: Mindestzahl der Ableitungsrunden (SQLCipher 4 nimmt von Haus aus 256'000).
#: Weniger heisst: jemand hat den Wert von Hand gesenkt, und das Erraten der
#: Passphrase wird um genau diesen Faktor billiger.
_MINDEST_RUNDEN = 256_000


def verschluesselung_pruefen() -> dict[str, str]:
    """Belegt beim Start, dass die Datenbank verschlüsselt auf der Platte liegt.

    **Warum das nötig ist.** Ob SQLCipher greift, hängt an Dingen, die niemand
    beim Start ansieht: ist ``sqlcipher3`` im Abbild? Kam das ``PRAGMA key``
    wirklich als erstes? Ist ``MONETEN_DB_KEY`` überhaupt gesetzt worden? Geht
    eines davon schief, läuft die App **weiter** — nur eben mit einer offenen
    Datei. Ein Fehler, der nichts kaputt macht und nichts meldet, bleibt Jahre.

    Der Test ist der einfachste, den es gibt: eine offene SQLite-Datei beginnt
    mit ``SQLite format 3``. Steht das dort, obwohl ein Schlüssel gesetzt ist,
    ist die Verschlüsselung nicht aktiv — dann **bricht der Start ab**, statt
    weitere Buchungen im Klartext dazuzuschreiben.

    Zusätzlich werden die Parameter gelesen und ins Protokoll geschrieben.
    Sie stehen sonst nirgends: die Frage „ist die Verschlüsselung stark genug"
    lässt sich ohne sie nur glauben, nicht beantworten.

    Gibt die gemessenen Werte zurück (leer, wenn kein Schlüssel gesetzt ist).
    """
    import logging

    from sqlalchemy.engine import make_url

    log = logging.getLogger("moneten")
    if not settings.db_key:
        return {}

    url = settings.database_url
    if url.startswith("sqlite"):
        pfad = make_url(url).database
        if pfad and pfad != ":memory:":
            datei = Path(pfad)
            if datei.is_file() and datei.open("rb").read(16) == _SQLITE_KOPF:
                raise RuntimeError(
                    f"Die Datenbank {datei} liegt UNVERSCHLÜSSELT auf der Platte, obwohl "
                    "MONETEN_DB_KEY gesetzt ist. Damit ist die Verschlüsselung nicht aktiv "
                    "(fehlendes sqlcipher3 im Abbild? falsche DB-URL?). Der Start wird "
                    "abgebrochen, damit nicht weiter im Klartext geschrieben wird."
                )

    werte: dict[str, str] = {}
    for pragma in ("cipher_version", "cipher_page_size", "kdf_iter", "cipher_hmac_algorithm"):
        try:
            with engine.connect() as conn:
                ergebnis = conn.exec_driver_sql(f"PRAGMA {pragma}").scalar()
            if ergebnis is not None:
                werte[pragma] = str(ergebnis)
        except Exception:  # noqa: BLE001 — ein unbekanntes PRAGMA darf den Start nicht kippen
            continue

    if werte:
        log.info("Verschlüsselung: %s", ", ".join(f"{k}={v}" for k, v in werte.items()))
    runden = werte.get("kdf_iter")
    if runden and runden.isdigit() and int(runden) < _MINDEST_RUNDEN:
        log.warning(
            "kdf_iter steht auf %s statt mindestens %s — das Erraten der Passphrase "
            "ist damit um denselben Faktor billiger.", runden, _MINDEST_RUNDEN,
        )
    return werte
