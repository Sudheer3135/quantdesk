"""Alembic environment.

The connection string comes from the application's own Settings object
rather than from alembic.ini. One definition, so a migration cannot be
applied to a different database than the one the app talks to.
"""
from logging.config import fileConfig

from alembic import context
from app.config import get_settings
from app.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A caller that has already set a URL (the migration tests, which run
# against a throwaway SQLite file) wins. Otherwise the application's own
# settings decide, so `alembic upgrade head` can never be pointed at a
# different database than the app talks to by editing an ini file.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER most things in place. Batch mode rebuilds
            # the table instead, which is the only way these migrations run
            # against the test database as well as against Postgres.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
