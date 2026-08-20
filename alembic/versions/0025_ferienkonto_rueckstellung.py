"""Treffen-Fonds: das Ferienkonto, auf das die Rücklage wirklich wandert.

Der Fonds war bis hier reine Planung — ein Klick je Monat bestätigte, dass Geld
zurückgelegt *wurde*, ohne dass irgendwo stand, wohin. Genau das ist die Lücke:
bestätigt ist schnell, überwiesen wird vergessen, und der Topf zeigt einen Stand,
den kein Konto deckt.

``holiday_account_id`` benennt das Konto, auf dem die Rückstellung liegt. Damit
lässt sich beides gegenrechnen: die bestätigten Rücklagen gegen die Zuflüsse auf
dem Konto (liegt das Geld da?) und die gerechneten Kosten eines vergangenen
Treffens gegen die Abflüsse davon (was hat es gekostet?).

Bewusst NULL-fähig und ohne Vorgabe: welches Konto gemeint ist, weiss nur der
Nutzer. Solange nichts gewählt ist, bleibt der Abgleich vollständig aus der
Oberfläche — ein Kasten, der „kein Konto gewählt" meldet, ist Fülltext.

Revision ID: 0025_ferienkonto_rueckstellung
Revises: 0024_lohn_hebung_zuruecknehmen
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_ferienkonto_rueckstellung"
down_revision: str | None = "0024_lohn_hebung_zuruecknehmen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meet_fund_settings") as batch:
        batch.add_column(sa.Column("holiday_account_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("meet_fund_settings") as batch:
        batch.drop_column("holiday_account_id")
