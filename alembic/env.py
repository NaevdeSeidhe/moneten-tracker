"""Alembic-Umgebung für den Moneten-Tracker.

Greift auf ``moneten.config.settings`` zu, damit die DB-URL aus der
``.env`` kommt und nicht in ``alembic.ini`` hartcodiert ist.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from moneten.config import settings
from moneten.db.models import Base  # importiert alle Models (Side-effect)

config = context.config

# DB-URL zur Laufzeit überschreiben (Wert aus alembic.ini ist nur Default).
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Erzeugt SQL ohne Datenbank-Verbindung (für ``alembic upgrade --sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Verbindet sich mit der echten DB und führt die Migrationen aus.

    Nutzt bewusst die **App-Engine** aus ``moneten.db.session`` — so greift bei
    gesetztem ``MONETEN_DB_KEY`` automatisch der SQLCipher-Pfad (Migrationen
    müssen die verschlüsselte Datei mit demselben Schlüssel öffnen).
    """
    from moneten.db.session import engine as connectable

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # nötig für SQLite-ALTER-TABLE
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
