from __future__ import annotations
import importlib
import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from pkgutil import iter_modules

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from app.core.database import Base

pkg = importlib.import_module("app.models")
if getattr(pkg, "__path__", None):
    for _, mod_name, _ in iter_modules(pkg.__path__, pkg.__name__ + "."):
        importlib.import_module(mod_name)
logger.debug("Imported model modules for Alembic autogenerate")

target_metadata = Base.metadata

def _get_url():
    env_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    from sqlalchemy.engine.url import make_url
    url_obj = make_url(env_url)
    if url_obj.drivername.endswith("+asyncpg"):
        url_obj = url_obj.set(drivername="postgresql+psycopg2")
    return str(url_obj)

config.set_main_option("sqlalchemy.url", _get_url())

def run_migrations_offline():
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        {"sqlalchemy.url": _get_url()},
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
