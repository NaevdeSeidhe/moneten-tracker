"""Start-PIN muss gewechselt werden: ``users.pin_changed_at``.

Die Start-PIN steht in der ``.env``. Wer die App aufsetzt und sie stehen lässt,
betreibt einen Finanzüberblick mit einer PIN, die in einer Beispieldatei
nachzulesen ist. Ist das Feld leer, lässt die App nur die Wechsel-Seite zu.

**Bestehende Installationen werden nicht grundlos belästigt.** Die Migration
prüft die gespeicherte PIN gegen die konfigurierte Start-PIN: passt sie NICHT,
wurde offensichtlich schon gewechselt, und das Feld bekommt einen Zeitstempel.
Passt sie, bleibt es leer — dann ist der Zwang genau richtig.

Geht die Prüfung nicht (kein Argon2, keine Konfiguration, unlesbarer Hash),
bleibt das Feld leer. Im Zweifel einmal zu viel fragen ist die harmlosere
Richtung: die andere hiesse, eine bekannte PIN stillschweigend durchzuwinken.

Revision ID: 0029_pin_wechsel_erzwingen
Revises: 0028_treffen_fonds_anonym
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0029_pin_wechsel_erzwingen"
down_revision: str | None = "0028_treffen_fonds_anonym"
branch_labels: str | None = None
depends_on: str | None = None


def _schon_gewechselt(pin_hash: str) -> bool:
    """Wurde die PIN nachweislich schon selbst gesetzt?

    Nachweis heisst hier: der gespeicherte Hash gehört NICHT zur konfigurierten
    Start-PIN. Ohne diesen Nachweis lautet die Antwort ``False`` — lieber einmal
    zu viel fragen.
    """
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerificationError, VerifyMismatchError

        from moneten.config import settings

        # Ohne konfigurierte Start-PIN gibt es nichts zu vergleichen: seit
        # 0.82.0 wird sie erst beim Anlegen des Benutzers gewuerfelt und
        # nirgends abgelegt. Dann lieber einmal zu viel fragen — das ist
        # dieselbe Regel, die im Kopf dieser Funktion steht.
        if not settings.initial_pin:
            return False

        try:
            PasswordHasher().verify(pin_hash, settings.initial_pin)
        except (VerifyMismatchError, VerificationError):
            return True   # Hash passt nicht zur Start-PIN → wurde gewechselt
        return False      # Hash passt → es gilt noch die Start-PIN
    except Exception:
        return False


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("pin_changed_at", sa.DateTime(timezone=True), nullable=True))

    bind = op.get_bind()
    # Der WIRKLICHE Wechselzeitpunkt ist nicht mehr feststellbar — er wurde nie
    # aufgeschrieben, deshalb gibt es diese Spalte ja. Eingetragen wird darum
    # „spätestens jetzt". Ein erfundenes Datum weit in der Vergangenheit stünde
    # später als Tatsache in der Oberfläche.
    jetzt = datetime.now(UTC)
    for user_id, pin_hash in bind.execute(sa.text("SELECT id, pin_hash FROM users")):
        if pin_hash and _schon_gewechselt(pin_hash):
            bind.execute(
                sa.text("UPDATE users SET pin_changed_at = :wann WHERE id = :id"),
                {"wann": jetzt, "id": user_id},
            )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("pin_changed_at")
