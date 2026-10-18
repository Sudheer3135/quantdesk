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
    # `disable_existing_loggers=False` is not a style preference. The default
    # is True, and it sets `.disabled = True` on every logger not named in
    # alembic.ini — which is every `app.*` logger this project has. Any
    # process that runs a migration in-process therefore goes permanently
    # silent afterwards: no agent tick, no collector failure, and no
    # scheduler-starvation alarm, all while the desk keeps running.
    #
    # Migrations run from `app.migrate` (scripts/migrate.sh, the Compose
    # `migrate` service), never inside the API process. The suite is: it
    # runs migrations in-process, and the tests proving the starvation alarm
    # actually logs were failing purely because alembic had muted the logger
    # several files earlier.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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


def _run_on(connection) -> None:
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


def run_migrations_online() -> None:
    # `app.migrate` hands over the connection that owns the exclusive schema
    # lock, and the migration runs on exactly that connection — no second
    # engine, no second connection. If it dies, the lock and the migration
    # die together (Pass 2E-A.2).
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run_on(supplied)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
