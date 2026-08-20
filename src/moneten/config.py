"""Konfiguration der App.

Sämtliche Einstellungen kommen aus Umgebungsvariablen (oder einer ``.env``-Datei).
Pydantic-Settings validiert die Werte beim Start; fehlt etwas Wichtiges, bricht die
App sofort mit klarer Fehlermeldung ab — kein stilles Fehlverhalten.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Bekannter Platzhalter — niemals zum Signieren echter Sessions verwenden.
_DEFAULT_SECRET = "change-me-immediately-please-use-a-random-48-byte-token"

_PRAEFIX = "MONETEN_"


def _sqlite_pfad(url: str) -> Path | None:
    """Die Datei hinter einer SQLite-Adresse — oder ``None`` bei allem anderen.

    ``sqlite:///./data/x.db`` ist relativ (drei Schrägstriche),
    ``sqlite:////app/data/x.db`` absolut (vier). Der Unterschied ist genau ein
    Zeichen und war schon zweimal die Ursache einer falschen Annahme, deshalb
    steht die Zerlegung EINMAL hier statt an jeder Fundstelle.
    """
    if not url.startswith("sqlite"):
        return None
    if url.startswith("sqlite:////"):
        return Path("/" + url.split("////", 1)[-1])
    if url.startswith("sqlite:///"):
        return Path(url.split("///", 1)[-1])
    return None



class Settings(BaseSettings):
    """Alle Laufzeit-Konfigurationen des Moneten-Trackers.

    Werte kommen aus ``.env`` und der Prozess-Umgebung. Präfix ``MONETEN_``
    macht den Ursprung in Logs sofort erkennbar.

    Bis v0.77.0 wurde zusätzlich das alte Präfix ``BILANZ_`` gelesen und auf das
    neue abgebildet — der Übergang für eine bestehende ``.env``. Die Abbildung
    ist entfernt, nachdem der Startlauf gemeldet hatte, welche Namen noch alt
    waren, und die Datei umgestellt worden ist.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=_PRAEFIX,
        extra="ignore",
    )

    # --- Sicherheit ------------------------------------------------------
    secret_key: str = Field(
        default=_DEFAULT_SECRET,
        description="Signier-Schlüssel für Session-Cookies. In Produktion zufällig setzen.",
    )

    initial_pin: str = Field(
        default="",
        description=(
            "PIN beim allerersten Start. LEER lassen: dann wird eine zufällige "
            "erzeugt und einmal ins Protokoll geschrieben."
        ),
    )

    # --- Datenbank & Speicherorte ---------------------------------------
    database_url: str = Field(
        default="sqlite:///./data/moneten.db",
        description="SQLAlchemy-DSN. Default zeigt auf eine lokale Datei für die Entwicklung.",
    )

    db_key: str | None = Field(
        default=None,
        description=(
            "Optionaler SQLCipher-Schlüssel. Ist er gesetzt (und das Paket "
            "'sqlcipher3-binary' installiert, im Docker-Image enthalten), wird die "
            "SQLite-Datei transparent verschlüsselt abgelegt. Leer = Klartext-DB "
            "(lokale Entwicklung/Tests)."
        ),
    )

    attachments_dir: Path = Field(
        default=Path("./data/attachments"),
        description="Wurzelverzeichnis für interne Daten-Anhänge. Wird beim Start angelegt, falls fehlend.",
    )

    anbieter_dir: Path = Field(
        default=Path("./data/anbieter"),
        description=(
            "Ordner mit eigenen Anbieterprofilen (*.toml) für Rechnungen mit "
            "aufgeschlüsselten Positionen. Liegt bei den DATEN und nicht beim Programm: "
            "so wandert er weder ins Abbild noch in ein Repository. Mitgelieferte "
            "Beispielprofile stehen daneben; bei gleichem Namen gewinnt die eigene Datei."
        ),
    )

    receipts_dir: Path | None = Field(
        default=None,
        description=(
            "Pfad zu deinem bestehenden Quittungs-/Rechnungs-Ordner (z.B. ScanSnap-Ablage). "
            "Die App kopiert NICHTS — sie referenziert nur die Dateinamen. Leer = keine Ordner-Anbindung."
        ),
    )

    # --- Betriebsmodus --------------------------------------------------
    root_path: str = Field(
        default="",
        description="Pfad-Präfix wenn die App hinter einem Reverse-Proxy mit Subpath läuft.",
    )

    proxy_hops: int = Field(
        default=1,
        ge=0,
        description=(
            "Wie viele Reverse-Proxys vor der App stehen. Daran haengt, welcher "
            "Eintrag von X-Forwarded-For als Absender der Login-Drossel gilt: "
            "jeder Proxy haengt seine Sicht hinten an, der letzte Eintrag stammt "
            "also vom naechstgelegenen Proxy und nicht vom Klopfenden. 0 = kein "
            "Proxy, dann wird der Header gar nicht angesehen."
        ),
    )

    log_level: str = Field(
        default="INFO",
        description="Logging-Level für die App. Akzeptiert DEBUG, INFO, WARNING, ERROR.",
    )

    ocr_lang: str = Field(
        default="deu+eng",
        description="Tesseract-Sprachen für den OCR-Fallback (z.B. 'deu+eng').",
    )

    timezone: str = Field(
        default="Europe/Zurich",
        description=(
            "Zeitzone für das heutige Datum. Der Container läuft in UTC — ohne "
            "diese Angabe zeigten Budget und Steuerseite zwischen Mitternacht "
            "und 02:00 den Vortag, also den falschen Monat bzw. das falsche Jahr."
        ),
    )

    dev_mode: bool = Field(
        default=False,
        description="Aktiviert lockerere Cookie-Flags und ausführlichere Fehler-Ausgabe lokal.",
    )

    # --- Session-Konfiguration -----------------------------------------
    session_cookie_name: str = "moneten_session"
    # Nach dieser Zeit OHNE Nutzung ist die Sitzung abgelaufen. Die Frist ist
    # gleitend: jede Seite, die du aufrufst, setzt sie zurück (siehe die
    # Middleware in main.py). Mitten im Arbeiten fliegt man also nicht raus.
    #
    # Vorher standen hier zwei Wochen, absolut ab dem Login — die App öffnete
    # sich danach vierzehn Tage lang ohne Nachfrage. Auf einem Handy, das man
    # aus der Hand gibt oder verliert, liegen damit alle Zahlen offen.
    #
    # Per Umgebungsvariable änderbar, ohne Deploy: MONETEN_SESSION_MAX_AGE_SECONDS
    session_max_age_seconds: int = 60 * 15  # 15 Minuten Leerlauf

    # Karenz für die Sperre beim Zurückkehren in die App (siehe app.js).
    # Sie muss lang genug sein, um ein Beleg-Foto zu schiessen: dabei wechselt
    # das Handy in die Kamera-App, die PWA geht in den Hintergrund. Ohne Karenz
    # wäre man beim Zurückkommen abgemeldet — mitsamt der Quittung.
    # Kurz genug, dass „Handy weggelegt" trotzdem sperrt.
    session_return_grace_seconds: int = 45

    @model_validator(mode="after")
    def _bestehende_datenbank_weiterverwenden(self) -> Settings:
        """Findet die Datenbank auch dann, wenn sie noch den alten Namen trägt.

        **Warum das hier stehen muss.** Beim Umbenennen des Programms änderte
        sich auch der STANDARDNAME der Datenbankdatei. Wer den Pfad in seiner
        ``.env`` gesetzt hat, merkt davon nichts. Wer ihn nicht gesetzt hat,
        bekäme beim nächsten Start eine frische, leere Datenbank neben der alten
        — die App läuft, alles scheint weg, und niemand erfährt warum. Genau
        diese lautlose Sorte Fehler soll eine Umbenennung nicht produzieren.

        Deshalb: zeigt der Standardpfad ins Leere und liegt daneben eine Datei
        mit dem alten Namen, wird die alte genommen. Die Meldung sagt es
        deutlich, damit man den Namen bei Gelegenheit angleichen kann.

        Nur für SQLite und nur für den STANDARD — wer einen Pfad ausdrücklich
        setzt, bekommt genau den. Eine Vermutung darf keine Angabe überstimmen.
        """
        standard = "sqlite:///./data/moneten.db"
        if self.database_url != standard:
            return self
        neu, alt = Path("./data/moneten.db"), Path("./data/bilanz.db")
        if not neu.exists() and alt.exists():
            self.database_url = f"sqlite:///./{alt.as_posix()}"
            logger.warning(
                "Datenbank %s gefunden, %s nicht — die bestehende wird weiter benutzt. "
                "Zum Angleichen: Datei umbenennen (dabei -wal und -shm mitnehmen "
                "oder vorher sauber beenden) oder MONETEN_DATABASE_URL setzen.",
                alt, neu,
            )
        return self

    @model_validator(mode="after")
    def _warnen_wenn_eine_andere_datenbank_daneben_liegt(self) -> Settings:
        """Sagt es, wenn die konfigurierte Datei fehlt und eine andere daneben liegt.

        **Der gemessene Fall.** Die Anleitung sagt ``cp .env.example .env``, und
        ``.env.example`` setzt einen ABSOLUTEN Pfad auf ``moneten.db``. Der
        Rückfall darüber greift dann nicht — richtig so, eine Vermutung darf
        keine Angabe überstimmen. Die Folge war aber, dass die App eine frische,
        leere Datenbank anlegte, während die volle mit dem alten Namen daneben
        lag: Anmeldeseite kommt, Konten sind da (aus den Vorgaben), Buchungen
        null. Kein Fehler, keine Meldung, alles scheint weg.

        Also wird hier nicht geraten, sondern gesagt. Beim ersten Start einer
        neuen Anlage liegt keine andere Datei daneben — dann bleibt es still.
        """
        pfad = _sqlite_pfad(self.database_url)
        if pfad is None:
            return self
        try:
            if pfad.exists():
                return self
            andere = sorted(p.name for p in pfad.parent.glob("*.db") if p.name != pfad.name)
        except OSError:
            return self
        if andere:
            logger.warning(
                "ACHTUNG: %s existiert nicht — es wird eine LEERE Datenbank angelegt. "
                "Im selben Ordner liegt aber: %s. Wenn deine Daten dort stehen, setze "
                "MONETEN_DATABASE_URL auf diese Datei, BEVOR du weiterarbeitest.",
                pfad, ", ".join(andere),
            )
        return self

    @model_validator(mode="after")
    def _warnen_wenn_die_datenbank_weit_weg_angelegt_wird(self) -> Settings:
        """Sagt es, wenn gleich ein ganzer Ordnerbaum an unerwarteter Stelle entsteht.

        **Der gemessene Fall.** ``.env.example`` setzt einen Pfad, der IM
        CONTAINER gilt (``/app/data/moneten.db``). Wer die Datei unverändert für
        einen Lauf auf dem eigenen Rechner benutzt, legt die Datenbank damit in
        einem ``/app/data`` an, das dort neu entsteht — unter Windows in der
        Wurzel des Laufwerks. Die App läuft, das
        Projekt-``data/`` bleibt leer, und wer das später merkt, hat seine
        Buchungen in einer Datei, die er nicht mehr sucht.

        Die Bedingung ist bewusst eng: gewarnt wird nur, wenn die Datei fehlt UND
        ihr Ordner noch gar nicht existiert UND er ausserhalb des Arbeits-
        verzeichnisses liegt. Im Container trifft das nie zu (``WORKDIR /app``,
        ``/app/data`` ist gemountet), und wer seine Daten bewusst auf ein anderes
        Volume legt, hat den Ordner längst.
        """
        pfad = _sqlite_pfad(self.database_url)
        if pfad is None:
            return self
        try:
            if pfad.exists() or pfad.parent.exists():
                return self
            ziel = pfad.parent.resolve()
            hier = Path.cwd().resolve()
            if ziel == hier or hier in ziel.parents:
                return self
        except OSError:
            return self
        logger.warning(
            "Die Datenbank wird unter %s angelegt — ausserhalb des Projekts, und "
            "der Ordner entsteht dabei neu. Sieht das nach einem Container-Pfad "
            "aus? Dann MONETEN_DATABASE_URL in der .env auskommentieren; ohne "
            "Angabe liegt die Datei unter ./data/.",
            pfad.parent.resolve(),
        )
        return self

    @model_validator(mode="after")
    def _resolve_secret_key(self) -> Settings:
        """Niemals mit dem bekannten Default-Schlüssel signieren.

        Ist ``MONETEN_SECRET_KEY`` nicht gesetzt, wird ein zufälliger Schlüssel
        einmalig erzeugt und unter ``data/secret_key`` persistiert (so bleiben
        Sessions über Neustarts gültig). Das macht die App **sicher by default**,
        ohne Konfigurations-Aufwand. Fällt das Schreiben aus, wird ein
        flüchtiger Zufallsschlüssel genutzt — immer noch besser als der Default.
        """
        if self.secret_key != _DEFAULT_SECRET:
            return self
        try:
            data_dir = Path(self.attachments_dir).parent
            data_dir.mkdir(parents=True, exist_ok=True)
            key_file = data_dir / "secret_key"
            if key_file.exists() and key_file.read_text(encoding="utf-8").strip():
                self.secret_key = key_file.read_text(encoding="utf-8").strip()
            elif key_file.exists():
                # **Leere Datei.** Vorher lief das still in einen fluechtigen
                # Schluessel: bei JEDEM Start ein anderer, also nach jedem
                # Neustart alle Sitzungen ungueltig — auf dem Handy heisst das
                # „ich muss mich staendig neu anmelden", und niemand kaeme auf
                # eine leere Datei als Ursache. Entstehen kann sie leicht: ein
                # abgebrochenes Schreiben, eine Ruecksicherung, ein Editor.
                # Jetzt wird sie gefuellt statt uebergangen.
                neuer = secrets.token_urlsafe(48)
                key_file.write_text(neuer, encoding="utf-8")
                os.chmod(key_file, 0o600)
                self.secret_key = neuer
                logger.warning(
                    "%s war leer — neuer Signier-Schluessel erzeugt. Bestehende "
                    "Sitzungen sind damit einmalig ungueltig.", key_file
                )
            else:
                new_key = secrets.token_urlsafe(48)
                # **Gleich eng anlegen statt nachträglich flicken.**
                # ``write_text`` öffnet mit 0666 abzüglich umask, im Container
                # also 0644 — weltlesbar. Die Datei liegt im gemounteten
                # Datenordner: auf dem NAS kann sie damit jedes Konto lesen, das
                # an die Freigabe kommt, und mit diesem Schlüssel lässt sich ein
                # Sitzungs-Cookie fälschen.
                #
                # ``O_EXCL``: zwei gleichzeitige Starts sollen sich nicht
                # überschreiben — der zweite landet im ``except OSError`` und
                # nimmt einen flüchtigen Schlüssel.
                with os.fdopen(
                    os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w", encoding="utf-8",
                ) as f:
                    f.write(new_key)
                self.secret_key = new_key
                logger.warning(
                    "Kein MONETEN_SECRET_KEY gesetzt — zufälliger Schlüssel in %s erzeugt.", key_file
                )
        except OSError:
            self.secret_key = secrets.token_urlsafe(48)
            logger.warning("Secret-Key konnte nicht persistiert werden — flüchtiger Schlüssel aktiv.")
        return self

    @model_validator(mode="after")
    def _start_pin_wuerfeln(self) -> Settings:
        """Ohne gesetzte Start-PIN eine zufällige erzeugen — und sie EINMAL sagen.

        **Warum keine feste Zahl mehr.** Vorher stand ``123456`` als Vorgabe im
        Code und unkommentiert in ``.env.example``. Wer die Anleitung befolgt
        (``cp .env.example .env && docker compose up -d``), betreibt seinen
        Finanzüberblick damit hinter einer PIN, die jeder im Repository nachliest.
        Der Zwang zum Wechsel hilft dagegen nicht: er greift beim ERSTEN Login —
        und der Erste ist im Zweifel nicht der Betreiber. Wer zuerst da ist,
        setzt seine eigene PIN und sperrt den Betreiber aus.

        Die zufällige PIN steht einmal im Protokoll (``docker compose logs``).
        Das ist derselbe Weg, den der Signier-Schlüssel oben schon geht.
        """
        # **Gewuerfelt wird nicht mehr hier.** Diese Klasse wird in JEDEM Prozess
        # gebaut, der die Konfiguration importiert — beim Start also mindestens
        # zweimal: einmal in ``alembic/env.py`` fuer die Migrationen und einmal
        # in der App. Beide zogen ihre eigene Zahl und meldeten sie, und im
        # Startprotokoll standen zwei verschiedene „Start-PINs" untereinander.
        # Die zuerst genannte gehoerte zum Migrationslauf und war tot: angelegt
        # wurde der Benutzer mit der zweiten. Wer die erste abschrieb, kam nicht
        # hinein und hatte keinen Grund, an der Zahl zu zweifeln.
        #
        # Gewuerfelt wird jetzt dort, wo der Benutzer wirklich entsteht — in
        # ``db.seeds`` ueber :func:`start_pin_erzeugen`. Das ist genau einmal
        # pro Anlage, und die gemeldete Zahl ist die, die auch gilt.
        return self


# Eine Modul-Singleton-Instanz, die überall importiert werden kann.
# Beim Test wird sie über dependency overrides ausgetauscht.
settings = Settings()


def start_pin_erzeugen() -> str:
    """Wuerfelt die Start-PIN und meldet sie **einmal** im Protokoll.

    Aufgerufen wird sie nur beim Anlegen des ersten Benutzers (``db.seeds``).
    Vorher stand das Wuerfeln in einem Validator der Konfiguration und lief
    damit in jedem Prozess, der sie importiert — mit zwei verschiedenen Zahlen
    im selben Startprotokoll als Folge.

    **Warum keine feste Zahl.** Vorher stand ``123456`` als Vorgabe im Code und
    unkommentiert in ``.env.example``. Wer die Anleitung befolgt, betreibt seinen
    Finanzueberblick damit hinter einer PIN, die jeder im Repository nachliest.
    Der Zwang zum Wechsel hilft dagegen nicht: er greift beim ERSTEN Login — und
    der Erste ist im Zweifel nicht der Betreiber.
    """
    pin = f"{secrets.randbelow(1_000_000):06d}"
    logger.warning(
        "Keine MONETEN_INITIAL_PIN gesetzt — Start-PIN fuer den ersten Login: %s "
        "(danach fragt die App sofort nach einer eigenen).", pin,
    )
    return pin
