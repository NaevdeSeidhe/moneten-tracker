"""attachments.file_path nullable (Quittung als reine Namens-Referenz).

Seit dem Umbau auf Ordner-Referenz wird teils nur der Dateiname vermerkt
(ohne konfigurierten Ordner) — dann gibt es keinen Pfad.

Revision ID: 0003_attachment_filepath_nullable
Revises: 0002_transfer_group
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_attachment_filepath_nullable"
down_revision: str | None = "0002_transfer_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.alter_column("file_path", existing_type=sa.String(500), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.alter_column("file_path", existing_type=sa.String(500), nullable=False)
