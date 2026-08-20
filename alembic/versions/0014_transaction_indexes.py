"""Performance: Indizes auf transactions(date, account_id, category_id).

Fast jede Abfrage (Dashboard, Buchungen, Budget, Vergleich, Prognose) filtert über
diese Spalten. Ohne Index muss SQLite jedes Mal die gesamte Buchungstabelle scannen —
auf dem NAS (schwache CPU + SQLCipher-Verschlüsselung) spürbar langsam. Rein additiv,
ändert keine Daten. Index-Namen entsprechen SQLAlchemys ``index=True``-Default
(``ix_<tabelle>_<spalte>``), damit Dev (create_all) und NAS (Migration) übereinstimmen.

Revision ID: 0014_transaction_indexes
Revises: 0013_receipt_photo_upload
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_transaction_indexes"
down_revision: str | None = "0013_receipt_photo_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # WICHTIG: ``ix_transactions_date`` wird bereits in 0001 (initiales Schema) angelegt —
    # hier NICHT erneut erstellen, sonst bricht „index already exists" die Migration ab,
    # und weil der Entrypoint (``set -e``, ``alembic upgrade head`` vor uvicorn) bei
    # Fehler stirbt, kommt der Container nicht hoch → 502. Hier nur die wirklich neuen
    # Indizes. ``IF NOT EXISTS`` als zusätzliche Absicherung gegen Teil-/Wiederholläufe
    # (SQLite-DDL ist nicht transaktional → ein abgebrochener Lauf lässt Reste zurück).
    op.execute("CREATE INDEX IF NOT EXISTS ix_transactions_account_id ON transactions (account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_transactions_category_id ON transactions (category_id)")


def downgrade() -> None:
    # ``ix_transactions_date`` NICHT droppen — gehört 0001, nicht dieser Migration.
    op.execute("DROP INDEX IF EXISTS ix_transactions_category_id")
    op.execute("DROP INDEX IF EXISTS ix_transactions_account_id")
