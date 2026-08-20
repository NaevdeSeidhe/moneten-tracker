"""Die Alembic-Migrationskette läuft auf einer FRISCHEN DB sauber durch.

Hintergrund: Das übrige Test-Setup baut das Schema mit ``create_all`` auf (schnell),
NICHT über Alembic. Dadurch blieb ein echter Migrationsfehler unentdeckt — 0014 legte
``ix_transactions_date`` an, das 0001 bereits erstellt hatte → „index already exists" —
und tauchte erst beim Prod-Deploy auf, wo der Entrypoint (``set -e``,
``alembic upgrade head`` vor uvicorn) den Container killte (502 Bad Gateway).

Dieser Test fährt exakt den Entrypoint-Pfad gegen eine leere SQLite-DB und hätte genau
diesen Fehler gefangen. Bewusst per Subprocess + eigener DB-URL, damit eine echte,
unabhängige Migration läuft (nicht die bereits via ``create_all`` aufgebaute Test-DB).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Projektwurzel (enthält alembic.ini); tests/ liegt eine Ebene darunter.
_ROOT = Path(__file__).resolve().parents[1]


def _alembic(*args: str, db_url: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("MONETEN_DB_KEY", None)  # reines SQLite, kein SQLCipher
    env["MONETEN_DATABASE_URL"] = db_url
    env.setdefault("MONETEN_SECRET_KEY", "test-secret-key")
    env.setdefault("MONETEN_INITIAL_PIN", "424242")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


def test_alembic_upgrade_head_on_fresh_db() -> None:
    """``alembic upgrade head`` muss eine leere DB ohne Fehler bis zum Head bringen."""
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'fresh.db'}"
        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"`alembic upgrade head` schlug fehl:\n{proc.stderr}"


def test_fonds_start_wandert_auf_den_jahresanfang() -> None:
    """0022 öffnet die Vergangenheit einer BESTEHENDEN Datenbank.

    Das Modell leitet die Vorgabe inzwischen ab — das gilt aber nur für Zeilen,
    die es noch nicht gibt. Wer den Treffen-Fonds schon benutzt, hat den festen
    1.7.2026 in der DB stehen und käme ohne diesen Schritt weiterhin genau einen
    Monat zurück. Geprüft wird darum am echten Migrationsweg: Zeile im alten
    Zustand anlegen, hochziehen, nachsehen.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'meet.db'}"
        proc = _alembic("upgrade", "0021_lohnzusammensetzung", db_url=url)
        assert proc.returncode == 0, f"Hochziehen auf 0021 schlug fehl:\n{proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO meet_fund_settings (id, start_month) VALUES (1, '2026-07-01')"
                ))
        finally:
            engine.dispose()

        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"`alembic upgrade head` schlug fehl:\n{proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                start = conn.scalar(text("SELECT start_month FROM meet_fund_settings"))
                spalten = conn.execute(text("PRAGMA table_info(meet_fund_settings)")).all()
        finally:
            engine.dispose()

    assert str(start)[:10] == "2026-01-01", f"Startmonat nach der Migration: {start}"
    # Und die zweite feste Vorgabe ist weg: sie stand als server_default im
    # Schema und hätte einen INSERT ohne Startmonat still auf 2026 gesetzt.
    vorgabe = [z[4] for z in spalten if z[1] == "start_month"]
    assert vorgabe == [None], f"start_month hat weiter eine feste Vorgabe im Schema: {vorgabe}"


def test_bestand_behauptet_keine_abgelesene_herkunft() -> None:
    """Kein Bestandsposten darf nach der Wanderung ``fortgeschrieben`` heissen.

    Die erste Zeile ist das, was pytest bei einem Fehlschlag zeigt — sie stand
    hier als das GEGENTEIL der geprüften Aussage („0023 hebt den bestehenden
    Bestand"), samt eines Satzes, der mitten im Wort abbrach. Beides ein Rest
    davon, dass der Test umgeschrieben und der Docstring nur halb mitgezogen
    wurde.

    0023 tat es trotzdem: es filterte auf ABRECHNUNGS-Ebene („die Grundlage nennt
    einen Herkunftsmonat") und hob danach JEDEN gerechneten Posten dieser
    Aufstellung. Beide Fälle hier lagen im Altbestand als ``gerechnet`` vor —
    die alte Übernahme schrieb abgelesene wie gerechnete Posten gleich weg. Es
    gibt im Bestand also gar kein Merkmal, an dem sich das trennen liesse; das
    Heben war ein Raten, und zwar in Richtung MEHR behaupteter Sicherheit.

    Dazu kam die freie Grundlage: „übernommen aus dem Vertrag" traf dasselbe
    Muster wie „übernommen aus April 2026".

    0024 setzt zurück. Die richtige Stufe je Posten entsteht beim nächsten
    Speichern — dort ist die Herkunft des einzelnen Postens bekannt.

    Alle Beträge sind erfunden.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'lohn.db'}"
        proc = _alembic("upgrade", "0022_fonds_start_jahresanfang", db_url=url)
        assert proc.returncode == 0, f"Hochziehen auf 0022 schlug fehl:\n{proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO accounts (id, name, type, currency, opening_balance,"
                    " current_balance, is_active, sort_order, created_at)"
                    " VALUES (1, 'Testkonto', 'bank', 'CHF', 0, 0, 1, 0, '2026-01-01')"
                ))
                for tx_id, tag in (
                    (1, "2026-05-25"), (2, "2026-06-25"), (3, "2026-07-25"),
                ):
                    conn.execute(text(
                        "INSERT INTO transactions (id, account_id, date, amount, description,"
                        " is_split, created_at, updated_at)"
                        f" VALUES ({tx_id}, 1, '{tag}', 5000, 'Lohn', 0,"
                        " '2026-01-01', '2026-01-01')"
                    ))
                for a_id, tx_id, grundlage in (
                    (1, 1, "übernommen aus April 2026"),
                    (2, 2, "Jahreslohn 2025 ÷ 12"),
                    (3, 3, "übernommen aus dem Vertrag"),
                ):
                    conn.execute(text(
                        "INSERT INTO lohn_abrechnungen (id, transaction_id, grundlage,"
                        " created_at, updated_at)"
                        f" VALUES ({a_id}, {tx_id}, '{grundlage}', '2026-01-01', '2026-01-01')"
                    ))
                    conn.execute(text(
                        "INSERT INTO lohn_posten (abrechnung_id, art, label, betrag,"
                        " herkunft, sort_order)"
                        f" VALUES ({a_id}, 'brutto', 'Monatslohn', 5000, 'gerechnet', 0)"
                    ))
        finally:
            engine.dispose()

        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"`alembic upgrade head` schlug fehl:\n{proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                stufen = dict(conn.execute(text(
                    "SELECT abrechnung_id, herkunft FROM lohn_posten"
                )).all())
        finally:
            engine.dispose()

    falsch = {a: s for a, s in stufen.items() if s != "gerechnet"}
    assert not falsch, (
        "Bestandsposten geben sich als abgelesen aus, obwohl der Bestand das "
        f"nicht hergibt: {falsch}")


def test_0024_nimmt_die_hebung_auf_ausgerollten_datenbanken_zurueck():
    """Was 0023 auf einer schon ausgerollten Datenbank angerichtet hat, geht weg.

    0023 war bereits ausgerollt; auf dem Server stehen die gehobenen Posten
    also da. 0023 zu entschärfen genügt darum nicht — die Korrektur muss den
    Bestand anfassen. Nachgestellt wird genau das: eine Datenbank auf Stand
    0023, in der ein Posten ``fortgeschrieben`` heisst.

    Alle Beträge sind erfunden.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'lohn.db'}"
        proc = _alembic("upgrade", "0023_lohn_fortgeschrieben", db_url=url)
        assert proc.returncode == 0, f"Hochziehen auf 0023 schlug fehl: {proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO accounts (id, name, type, currency, opening_balance,"
                    " current_balance, is_active, sort_order, created_at)"
                    " VALUES (1, 'Testkonto', 'bank', 'CHF', 0, 0, 1, 0, '2026-01-01')"
                ))
                conn.execute(text(
                    "INSERT INTO transactions (id, account_id, date, amount, description,"
                    " is_split, created_at, updated_at)"
                    " VALUES (1, 1, '2026-05-25', 5000, 'Lohn', 0,"
                    " '2026-01-01', '2026-01-01')"
                ))
                conn.execute(text(
                    "INSERT INTO lohn_abrechnungen (id, transaction_id, grundlage,"
                    " created_at, updated_at)"
                    " VALUES (1, 1, 'übernommen aus April 2026', '2026-01-01', '2026-01-01')"
                ))
                conn.execute(text(
                    "INSERT INTO lohn_posten (abrechnung_id, art, label, betrag,"
                    " herkunft, sort_order)"
                    " VALUES (1, 'brutto', 'Monatslohn', 5000, 'fortgeschrieben', 0)"
                ))
        finally:
            engine.dispose()

        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"`alembic upgrade head` schlug fehl: {proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                stufen = [s for (s,) in conn.execute(text(
                    "SELECT herkunft FROM lohn_posten")).all()]
        finally:
            engine.dispose()

    assert stufen == ["gerechnet"], (
        f"Die falsche Hebung steht auf der ausgerollten Datenbank weiter: {stufen}")


def _describe_diff(diff: tuple) -> str:
    kind = diff[0]
    if kind in ("add_table", "remove_table"):
        return f"{kind}: {diff[1].name}"
    if kind in ("add_column", "remove_column"):
        return f"{kind}: {diff[2]}.{diff[3].name}"
    return str(diff)


def test_migrations_match_models() -> None:
    """Das via Alembic migrierte Schema deckt sich strukturell mit ``models.py``.

    Fängt die Bug-Klasse ab, bei der eine Modell-Änderung ohne passende Migration
    (oder umgekehrt) einläuft — eine Tabelle/Spalte, die ``create_all`` kennt, für die
    aber keine Migration existiert (oder andersrum). Solche Drifts bleiben sonst
    unsichtbar, weil das übrige Test-Setup das Schema per ``create_all`` baut statt
    über Alembic. Verglichen werden nur **strukturelle** Unterschiede (Tabellen/
    Spalten) — Typ-/Index-/Default-Feinheiten meldet SQLite-Autogenerate gern als
    Rauschen, das hier bewusst ignoriert wird.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from moneten.db.models import Base

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'head.db'}"
        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"`alembic upgrade head` schlug fehl:\n{proc.stderr}"

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        finally:
            engine.dispose()

    structural = {"add_table", "remove_table", "add_column", "remove_column"}
    relevant = [
        _describe_diff(d)
        for group in diffs
        for d in (group if isinstance(group, list) else [group])
        if isinstance(d, tuple) and d and d[0] in structural
    ]
    assert not relevant, (
        "Schema-Drift zwischen Migrationen (head) und models.py:\n  " + "\n  ".join(relevant)
    )


def _fonds_im_alten_zustand(url: str, a: str, b: str, ort_b: str, ort_a: str) -> None:
    """Stellt eine 0027-Datenbank auf die alte, personengebundene Benennung um.

    0015 legt die Spalten inzwischen neutral an — eine frisch migrierte DB sieht
    also nie so aus wie eine gewachsene. Genau diese gewachsene wird hier
    nachgebaut, sonst prüfte der Test den Fall nicht, für den 0028 existiert.
    """
    from sqlalchemy import create_engine, text

    motor = create_engine(url)
    with motor.begin() as c:
        for alt, neu in (
            ("monthly_a_chf", f"monthly_{a}_chf"), ("monthly_b_eur", f"monthly_{b}_eur"),
            ("flight_a_chf", f"flight_{a}_chf"), ("flight_b_chf", f"flight_{b}_chf"),
        ):
            c.execute(text(f"ALTER TABLE meet_fund_settings RENAME COLUMN {alt} TO {neu}"))
        c.execute(text(
            f"INSERT INTO meet_fund_settings (id, monthly_{a}_chf, monthly_{b}_eur, eur_chf_rate,"
            f" flight_{a}_chf, flight_{b}_chf, airbnb_night_chf, food_day_chf, default_nights,"
            " start_month, start_balance_chf)"
            " VALUES (1, 300, 100, 0.93, 350, 350, 116, 30, 3, '2026-01-01', 0)"
        ))
        for monat, wer, betrag in (("2026-02-01", a, 300), ("2026-02-01", b, 100),
                                   ("2026-03-01", a, 300)):
            c.execute(
                text("INSERT INTO meet_contributions (month, person, amount_native, confirmed_at)"
                     " VALUES (:m, :p, :b, '2026-03-01 12:00:00')"),
                {"m": monat, "p": wer, "b": betrag},
            )
        for datum, ort in (("2026-04-10", ort_b), ("2026-06-20", ort_a)):
            c.execute(
                text("INSERT INTO meet_visits (date, location, nights) VALUES (:d, :o, 3)"),
                {"d": datum, "o": ort},
            )
    motor.dispose()


def test_0028_liest_die_alten_schluessel_aus_dem_schema(monkeypatch) -> None:
    """Die Migration übersetzt Personen, ohne einen einzigen Namen zu kennen.

    Sie stünden sonst im Quelltext — verschoben, nicht entfernt. Der Test gibt
    ihr darum Schlüssel, die sie unmöglich eingebaut haben kann, und verlangt
    trotzdem eine vollständige Übersetzung.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'gewachsen.db'}"
        assert _alembic("upgrade", "0027_artikel_alias", db_url=url).returncode == 0
        _fonds_im_alten_zustand(url, "kolibri", "nashorn", "inselstadt", "bergdorf")

        monkeypatch.setenv("MONETEN_ALTE_ORTE", "inselstadt,bergdorf")
        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode == 0, f"0028 schlug fehl:\n{proc.stderr}"

        motor = create_engine(url)
        with motor.begin() as c:
            spalten = {r[1] for r in c.execute(text("PRAGMA table_info(meet_fund_settings)"))}
            assert {"monthly_a_chf", "monthly_b_eur", "flight_a_chf", "flight_b_chf",
                    "name_a", "name_b"} <= spalten
            assert not any("kolibri" in s or "nashorn" in s for s in spalten)
            # Die Beträge stehen noch da, wo sie hingehören.
            zeile = c.execute(text(
                "SELECT monthly_a_chf, monthly_b_eur, name_a, name_b FROM meet_fund_settings"
            )).one()
            assert (int(zeile[0]), int(zeile[1]), zeile[2], zeile[3]) == (300, 100, "Ich", "Partner")
            personen = sorted(r[0] for r in c.execute(text("SELECT person FROM meet_contributions")))
            assert personen == ["a", "a", "b"]
            orte = sorted(r[0] for r in c.execute(text("SELECT location FROM meet_visits")))
            assert orte == ["bei_a", "bei_b"]
        motor.dispose()


def test_0028_bricht_ab_statt_treffen_zu_verlieren(monkeypatch) -> None:
    """Unbekannte Ortsnamen sind ein Abbruch, kein stilles Weiterlaufen.

    Ohne diesen Riegel liefe die Migration durch, die Treffen behielten ihre
    alten Werte, und der Kalender wäre leer — bei vollständiger Tabelle. Ein
    Fehler, der nichts sagt, ist schlimmer als einer, der abbricht.
    """
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'ohne_hinweis.db'}"
        assert _alembic("upgrade", "0027_artikel_alias", db_url=url).returncode == 0
        _fonds_im_alten_zustand(url, "kolibri", "nashorn", "inselstadt", "bergdorf")

        monkeypatch.delenv("MONETEN_ALTE_ORTE", raising=False)
        proc = _alembic("upgrade", "head", db_url=url)
        assert proc.returncode != 0, "Die Migration lief durch, obwohl sie die Orte nicht kannte"
        assert "MONETEN_ALTE_ORTE" in proc.stderr
        assert "inselstadt" in proc.stderr, "Die Meldung nennt nicht, woran es lag"


def test_0028_laesst_nach_einem_fehlversuch_nichts_liegen(monkeypatch) -> None:
    """Ein Abbruch darf die Datenbank nicht festsetzen.

    **Der gemessene Fall.** Die Ortsprüfung stand am ENDE der Migration — also
    hinter ``batch_alter_table``. SQLite kennt für DDL keine Transaktion: die
    Hilfstabelle ``_alembic_tmp_meet_fund_settings`` war festgeschrieben und
    blieb liegen. Der nächste Lauf scheiterte daran mit „table already exists",
    **auch der richtige mit gesetzter Umgebungsvariable**. Auf dem NAS fährt der
    Entrypoint die Migration unter ``set -e`` vor dem Server, Compose startet
    endlos neu — und die Fehlermeldung riet zu genau dem Schritt, der nicht mehr
    ging.

    Geprüft wird deshalb der ganze Ablauf: scheitern, dann richtig machen.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'abbruch.db'}"
        assert _alembic("upgrade", "0027_artikel_alias", db_url=url).returncode == 0
        _fonds_im_alten_zustand(url, "kolibri", "nashorn", "inselstadt", "bergdorf")

        monkeypatch.delenv("MONETEN_ALTE_ORTE", raising=False)
        gescheitert = _alembic("upgrade", "head", db_url=url)
        assert gescheitert.returncode != 0, "die Migration hätte abbrechen müssen"

        motor = create_engine(url)
        with motor.begin() as c:
            reste = [r[0] for r in c.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_alembic_tmp_%'"
            ))]
            revision = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        motor.dispose()
        assert reste == [], f"Hilfstabelle blieb liegen: {reste}"
        assert revision == "0027_artikel_alias", "die Revision darf sich nicht verschoben haben"

        # Und jetzt der richtige Weg — er MUSS gehen.
        monkeypatch.setenv("MONETEN_ALTE_ORTE", "inselstadt,bergdorf")
        zweiter = _alembic("upgrade", "head", db_url=url)
        assert zweiter.returncode == 0, f"zweiter Versuch scheiterte:\n{zweiter.stderr[-800:]}"

        motor = create_engine(url)
        with motor.begin() as c:
            orte = sorted(r[0] for r in c.execute(text("SELECT location FROM meet_visits")))
            personen = sorted(r[0] for r in c.execute(text("SELECT person FROM meet_contributions")))
        motor.dispose()
        assert orte == ["bei_a", "bei_b"]
        assert personen == ["a", "a", "b"]


def test_0028_raeumt_eine_alte_hilfstabelle_selbst_weg(monkeypatch) -> None:
    """Wer schon eine festgesetzte Datenbank hat, kommt ohne SQL wieder heraus.

    Der Riegel oben verhindert neue Fälle. Dieser Test deckt die Datenbanken ab,
    die den Fehler schon erlebt haben — dort liegt die Hilfstabelle bereits, und
    ohne Selbstheilung müsste jemand von Hand ein ``DROP TABLE`` eintippen.
    """
    from sqlalchemy import create_engine, text

    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d) / 'festgesetzt.db'}"
        assert _alembic("upgrade", "0027_artikel_alias", db_url=url).returncode == 0
        _fonds_im_alten_zustand(url, "kolibri", "nashorn", "inselstadt", "bergdorf")

        motor = create_engine(url)
        with motor.begin() as c:
            c.execute(text("CREATE TABLE _alembic_tmp_meet_fund_settings (id INTEGER)"))
        motor.dispose()

        monkeypatch.setenv("MONETEN_ALTE_ORTE", "inselstadt,bergdorf")
        ergebnis = _alembic("upgrade", "head", db_url=url)
        assert ergebnis.returncode == 0, f"blieb festgesetzt:\n{ergebnis.stderr[-800:]}"


# ---------------------------------------------------------------------------
# 0030 — Bank-Referenz
# ---------------------------------------------------------------------------
def test_0030_ist_wiederholbar() -> None:
    """Hin, zurück, wieder hin — ohne „duplicate column" und ohne Handarbeit.

    Aus 0028 gelernt: eine Migration, die beim zweiten Lauf scheitert, sperrt die
    App auf dem NAS aus. Der Entrypoint ruft ``alembic upgrade head`` bei JEDEM
    Start; bricht das ab, killt ``set -e`` den Container, und die App kommt nicht
    mehr hoch. Deshalb wird hier nicht nur einmal migriert.
    """
    import sqlite3

    with tempfile.TemporaryDirectory() as d:
        datei = Path(d) / "ref.db"
        url = f"sqlite:///{datei}"
        assert _alembic("upgrade", "head", db_url=url).returncode == 0

        def _spalten() -> set[str]:
            con = sqlite3.connect(datei)
            try:
                return {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
            finally:
                con.close()

        assert "bank_reference" in _spalten()

        # Nochmals nach oben: darf nichts tun und nicht scheitern.
        assert _alembic("upgrade", "head", db_url=url).returncode == 0

        zurueck = _alembic("downgrade", "0029_pin_wechsel_erzwingen", db_url=url)
        assert zurueck.returncode == 0, f"downgrade schlug fehl: {zurueck.stderr}"
        assert "bank_reference" not in _spalten(), "downgrade liess die Spalte stehen"

        wieder = _alembic("upgrade", "head", db_url=url)
        assert wieder.returncode == 0, f"zweites upgrade schlug fehl: {wieder.stderr}"
        assert "bank_reference" in _spalten()


def test_0030_laesst_bestehende_buchungen_in_ruhe() -> None:
    """Bestehende Buchungen behalten ihren Inhalts-Hash und bekommen keine Referenz.

    Wäre die Spalte mit irgendeinem Ersatzwert gefüllt worden, hätte der Import
    den Altbestand für „schon mit Referenz importiert" gehalten und beim nächsten
    Auszug alles doppelt angelegt.
    """
    import sqlite3

    with tempfile.TemporaryDirectory() as d:
        datei = Path(d) / "alt.db"
        url = f"sqlite:///{datei}"
        assert _alembic("upgrade", "0029_pin_wechsel_erzwingen", db_url=url).returncode == 0

        # Rohes SQL, weil die Modelle den Stand NACH der Migration beschreiben.
        # ``created_at``/``updated_at`` sind NOT NULL ohne Vorgabe — der Wert ist
        # erfunden und ohne Bedeutung für den Test.
        stempel = "2026-07-14 08:00:00"
        con = sqlite3.connect(datei)
        try:
            con.execute(
                "INSERT INTO accounts (name, type, currency, opening_balance,"
                " current_balance, is_active, sort_order, created_at)"
                " VALUES ('Erfundenes Konto', 'BANK', 'CHF', 0, 0, 1, 1, ?)",
                (stempel,),
            )
            con.execute(
                "INSERT INTO transactions (account_id, date, amount, description,"
                " dedup_hash, created_at, updated_at)"
                " VALUES (1, '2026-07-14', -12.5, 'Erfundener Altposten',"
                " 'hash-alt-0030', ?, ?)",
                (stempel, stempel),
            )
            con.commit()
        finally:
            con.close()

        assert _alembic("upgrade", "head", db_url=url).returncode == 0

        con = sqlite3.connect(datei)
        try:
            zeile = con.execute(
                "SELECT dedup_hash, bank_reference FROM transactions WHERE description = ?",
                ("Erfundener Altposten",),
            ).fetchone()
        finally:
            con.close()
        assert zeile == ("hash-alt-0030", None), f"Buchung wurde verändert: {zeile}"

