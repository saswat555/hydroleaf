# alembic/env.py

from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from alembic import context

# pull in your app's settings and MetaData
from app.core.config import DATABASE_URL
from app.core.database import Base

# this is the Alembic Config object, which provides
# access to values within the .ini file in use.
config = context.config

# set up Python logging per the .ini file
if config.config_file_name:
    fileConfig(config.config_file_name)

# override the URL so we use the synchronous driver
# (alembic doesn't run inside an async loop)
sync_url = DATABASE_URL.replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", sync_url)

# tell Alembic about all of your models
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
