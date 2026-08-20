"""Treffen-Fonds: Personen heissen im Schema A und B statt mit Namen.

Die Spalten trugen die Vornamen zweier Menschen, das Feld ``person`` ihre
Kurzform, und ``location`` die beiden Länder. Ein Schema ist aber kein
Notizzettel: es steht im Quelltext, in jeder Migration und in jedem Backup. Wer
es liest, erfährt eine Beziehung, zwei Länder und Monatsbeträge.

**Die Daten bleiben vollständig.** Spalten werden umbenannt, nicht neu angelegt;
die Werte in ``person`` und ``location`` werden übersetzt. Wer die App vorher
benutzt hat, sieht danach dieselben Zahlen — nur unter anderen Bezeichnern.

**Warum in dieser Datei kein einziger Name steht.** Eine Migration, die
``WHERE person = '<Vorname>'`` schreibt, hätte den Namen nur verschoben: aus den
Spalten in den Quelltext. Die alten Schlüssel werden deshalb zur Laufzeit aus
dem Schema GELESEN — die alten Spalten hiessen ``monthly_<schlüssel>_chf`` und
``monthly_<schlüssel>_eur``, und genau diese Schlüssel standen auch in
``person``. Die Datenbank weiss also selbst, wie sie hiess; die Datei muss es
nicht wissen.

Die Anzeigenamen ziehen in zwei neue Spalten. Damit stehen sie in den DATEN und
nicht mehr im Code; dieselbe Trennung wie beim Anbieterprofil.

Revision ID: 0028_treffen_fonds_anonym
Revises: 0027_artikel_alias
"""

from __future__ import annotations

import os
import re

import sqlalchemy as sa

from alembic import op

revision: str = "0028_treffen_fonds_anonym"
down_revision: str | None = "0027_artikel_alias"
branch_labels: str | None = None
depends_on: str | None = None

# Die alten Spalten folgten diesem Bau. Aus ihm fällt der Personen-Schlüssel.
_ALT_A = re.compile(r"^monthly_(?!a_chf$)(\w+)_chf$")
_ALT_B = re.compile(r"^monthly_(?!b_eur$)(\w+)_eur$")

# Wer sich in einer früheren Fassung getroffen hat, hatte in ``location`` zwei
# Ortsnamen stehen. Die lassen sich NICHT aus dem Schema ableiten, deshalb hier
# als Umgebungsvariable für den einen Lauf:
#     MONETEN_ALTE_ORTE="<ort_von_b>,<ort_von_a>"
# Fehlt sie und stehen noch alte Werte in der Tabelle, bricht die Migration ab
# und sagt, welche. Stillschweigend weiterlaufen wäre das Schlimmste: die App
# fände die Treffen nicht mehr und zeigte einen leeren Kalender, obwohl sie in
# der Tabelle stehen.
_UMGEBUNG_ORTE = "MONETEN_ALTE_ORTE"


def _alte_schluessel(bind: sa.engine.Connection) -> tuple[str | None, str | None]:
    """Liest die alten Personen-Schlüssel aus den Spaltennamen."""
    spalten = [s["name"] for s in sa.inspect(bind).get_columns("meet_fund_settings")]
    a = next((m.group(1) for s in spalten if (m := _ALT_A.match(s))), None)
    b = next((m.group(1) for s in spalten if (m := _ALT_B.match(s))), None)
    return a, b


def _aufraeumen(bind: sa.engine.Connection) -> None:
    """Hilfstabellen eines früher abgebrochenen Laufs entfernen.

    **Warum das hier stehen muss.** SQLite kennt keine Transaktion für DDL —
    pysqlite fährt sie im Autocommit. Bricht diese Migration nach dem
    ``batch_alter_table`` ab, ist die Hilfstabelle festgeschrieben und bleibt
    liegen. Der nächste Lauf scheitert dann an „table already exists", und zwar
    AUCH der richtige. Nachgemessen: ohne diese Zeilen war die Datenbank nach
    einem Fehlversuch nicht mehr migrierbar, der Container lief auf dem NAS in
    eine Neustartschleife, und die Fehlermeldung riet zu genau dem Schritt, der
    nicht mehr ging.
    """
    for (name,) in bind.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_alembic_tmp_%'"
    )).fetchall():
        bind.execute(sa.text(f'DROP TABLE "{name}"'))


def upgrade() -> None:
    bind = op.get_bind()
    _aufraeumen(bind)

    # ---------------------------------------------------------------------
    # ZUERST prüfen, DANN anfassen.
    # ---------------------------------------------------------------------
    # Die Ortsnamen sind der einzige Teil, den diese Migration nicht selbst
    # herleiten kann. Die Prüfung stand früher am ENDE — nach dem Umbau des
    # Schemas. Das war die eigentliche Falle: der Abbruch kam, wenn die
    # Hilfstabelle schon existierte. Jetzt scheitert der Lauf, bevor eine
    # einzige Spalte angefasst ist, und ein zweiter Versuch findet dieselbe
    # Datenbank vor wie der erste.
    orte = [o.strip() for o in os.environ.get(_UMGEBUNG_ORTE, "").split(",") if o.strip()]
    if orte and len(orte) != 2:
        # Nicht stillschweigend die ueberzaehligen ignorieren: wer drei Orte
        # angibt, hat sich etwas gedacht, und der dritte bliebe sonst unuebersetzt
        # in der Tabelle stehen — sichtbar erst Monate spaeter im leeren Kalender.
        raise RuntimeError(
            f"{_UMGEBUNG_ORTE} braucht genau zwei Werte "
            f'("<ort_der_zweiten_person>,<eigener_ort>"), bekommen: {orte}'
        )
    unbekannt = sorted(
        r[0] for r in bind.execute(sa.text(
            "SELECT DISTINCT location FROM meet_visits "
            "WHERE location NOT IN ('bei_a', 'bei_b')"
        ))
        if r[0] not in orte
    )
    if unbekannt:
        raise RuntimeError(
            f"meet_visits.location enthält noch {unbekannt}. Diese Migration weiss nicht, "
            f"welcher Ort zu wem gehört. Einmalig setzen und erneut migrieren:\n"
            f'  {_UMGEBUNG_ORTE}="<ort_der_zweiten_person>,<eigener_ort>"\n'
            f"Am Schema wurde nichts geändert."
        )

    # ``location`` sagt jetzt, WO man sich trifft, statt in welchem Land.
    for alt, neu in zip(orte, ("bei_b", "bei_a"), strict=False):
        bind.execute(
            sa.text("UPDATE meet_visits SET location = :neu WHERE location = :alt"),
            {"neu": neu, "alt": alt},
        )

    alt_a, alt_b = _alte_schluessel(bind)

    # SQLite kann Spalten nur über eine Hilfstabelle umbenennen; ``batch_alter_table``
    # nimmt Alembic diese Arbeit ab und lässt den Code auf jeder Datenbank gleich.
    with op.batch_alter_table("meet_fund_settings") as batch:
        if alt_a:
            batch.alter_column(f"monthly_{alt_a}_chf", new_column_name="monthly_a_chf")
            batch.alter_column(f"flight_{alt_a}_chf", new_column_name="flight_a_chf")
        if alt_b:
            batch.alter_column(f"monthly_{alt_b}_eur", new_column_name="monthly_b_eur")
            batch.alter_column(f"flight_{alt_b}_chf", new_column_name="flight_b_chf")
        # Anzeigenamen. ``server_default`` ist nötig, weil bestehende Zeilen
        # sonst NULL bekämen und die Oberfläche eine leere Beschriftung zeigte.
        batch.add_column(sa.Column("name_a", sa.String(40), nullable=False, server_default="Ich"))
        batch.add_column(sa.Column("name_b", sa.String(40), nullable=False, server_default="Partner"))

    # Die gespeicherten Werte übersetzen. Ohne diesen Schritt stünden die
    # Rücklagen weiter unter den alten Schlüsseln — der Dienst fände sie nicht
    # mehr und zeigte einen leeren Topf, obwohl das Geld in der Tabelle steht.
    for alt, neu in ((alt_a, "a"), (alt_b, "b")):
        if alt:
            bind.execute(
                sa.text("UPDATE meet_contributions SET person = :neu WHERE person = :alt"),
                {"neu": neu, "alt": alt},
            )

    # Die Orte sind oben schon übersetzt — vor dem ersten DDL, damit ein
    # fehlender Wert nichts halb umgebaut zurücklässt.


def downgrade() -> None:
    """Zurück auf A/B-freie Bezeichner, soweit ohne Namen möglich.

    Die alten Vornamen sind nach dem Upgrade nirgends mehr gespeichert — dieser
    Weg zurück kann sie also nicht erraten. Er stellt die Spaltenstruktur wieder
    her und benennt die Personen dabei ``person_a``/``person_b``. Die Zahlen
    bleiben vollständig; nur die alte Beschriftung ist weg, und die stand mit
    Absicht nicht mehr im Quelltext.
    """
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE meet_contributions SET person = 'person_a' WHERE person = 'a'"))
    bind.execute(sa.text("UPDATE meet_contributions SET person = 'person_b' WHERE person = 'b'"))
    with op.batch_alter_table("meet_fund_settings") as batch:
        batch.drop_column("name_b")
        batch.drop_column("name_a")
        batch.alter_column("monthly_a_chf", new_column_name="monthly_person_a_chf")
        batch.alter_column("monthly_b_eur", new_column_name="monthly_person_b_eur")
        batch.alter_column("flight_a_chf", new_column_name="flight_person_a_chf")
        batch.alter_column("flight_b_chf", new_column_name="flight_person_b_chf")
