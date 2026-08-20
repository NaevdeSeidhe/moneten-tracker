"""Initial-Schema gemäss Abschnitt 5 der Spezifikation.

Erzeugt alle 12 Tabellen in einem Rutsch.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, server_default="Ich"),
        sa.Column("pin_hash", sa.String(255), nullable=False),
        sa.Column("webauthn_credentials_json", sa.Text, nullable=True),
        sa.Column("preferred_theme", sa.String(8), nullable=False, server_default="dark"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # 2) accounts
    # ------------------------------------------------------------------
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CHF"),
        sa.Column("opening_balance", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("current_balance", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("iban", sa.String(34), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("color", sa.String(9), nullable=True),
        sa.Column("icon", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # 3) categories
    # ------------------------------------------------------------------
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "parent_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("icon", sa.String(40), nullable=True),
        sa.Column("color", sa.String(9), nullable=True),
        sa.Column("management_type", sa.String(4), nullable=False),
        sa.Column("is_subscription", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_archived", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    # ------------------------------------------------------------------
    # 4) tags
    # ------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("color", sa.String(9), nullable=True),
    )

    # ------------------------------------------------------------------
    # 5) recurring_templates
    # ------------------------------------------------------------------
    op.create_table(
        "recurring_templates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("frequency", sa.String(12), nullable=False, server_default="monthly"),
        sa.Column("day_of_month", sa.Integer, nullable=True),
        sa.Column("next_due_date", sa.Date, nullable=True),
        sa.Column("auto_post", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_subscription", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    # ------------------------------------------------------------------
    # 6) import_batches
    # ------------------------------------------------------------------
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("period_from", sa.Date, nullable=True),
        sa.Column("period_to", sa.Date, nullable=True),
        sa.Column("total_transactions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("auto_categorized_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expected_closing_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("actual_closing_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("balance_match", sa.Boolean, nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
    )

    # ------------------------------------------------------------------
    # 7) transactions
    # ------------------------------------------------------------------
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("management_type", sa.String(4), nullable=True),
        sa.Column("attachment_path", sa.String(500), nullable=True),
        sa.Column(
            "recurring_template_id",
            sa.Integer,
            sa.ForeignKey("recurring_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "import_batch_id",
            sa.Integer,
            sa.ForeignKey("import_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "parent_transaction_id",
            sa.Integer,
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("is_split", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("dedup_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transactions_dedup_hash", "transactions", ["dedup_hash"])
    op.create_index("ix_transactions_date", "transactions", ["date"])
    op.create_index("ix_transactions_account_date", "transactions", ["account_id", "date"])

    # ------------------------------------------------------------------
    # 8) transaction_tags (Junction)
    # ------------------------------------------------------------------
    op.create_table(
        "transaction_tags",
        sa.Column(
            "transaction_id",
            sa.Integer,
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer,
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ------------------------------------------------------------------
    # 9) budgets
    # ------------------------------------------------------------------
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("planned_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("is_auto_calculated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint("category_id", "month", name="uq_budgets_cat_month"),
    )

    # ------------------------------------------------------------------
    # 10) savings_goals
    # ------------------------------------------------------------------
    op.create_table(
        "savings_goals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("target_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("target_date", sa.Date, nullable=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("priority", sa.String(8), nullable=False, server_default="medium"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon", sa.String(40), nullable=True),
        sa.Column("is_achieved", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # 11) categorization_rules
    # ------------------------------------------------------------------
    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("conditions_json", sa.Text, nullable=False),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # 12) attachments
    # ------------------------------------------------------------------
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "transaction_id",
            sa.Integer,
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=True),
        sa.Column("original_name", sa.String(255), nullable=True),
        sa.Column("ocr_text", sa.Text, nullable=True),
        sa.Column("parsed_items_json", sa.Text, nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop in umgekehrter Reihenfolge wegen Foreign-Keys."""
    op.drop_table("attachments")
    op.drop_table("categorization_rules")
    op.drop_table("savings_goals")
    op.drop_table("budgets")
    op.drop_table("transaction_tags")
    op.drop_index("ix_transactions_account_date", table_name="transactions")
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_index("ix_transactions_dedup_hash", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("import_batches")
    op.drop_table("recurring_templates")
    op.drop_table("tags")
    op.drop_table("categories")
    op.drop_table("accounts")
    op.drop_table("users")
