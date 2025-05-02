# alembic/env.py
"""Alembic configuration for Hydroleaf

* Loads `DATABASE_URL` from the environment (or alembic.ini) and injects it
  into Alembic.
* Imports every module under **app.models** so that all `Table` objects are
  registered on the project's single declarative `Base`.
* Works both in **offline** (generate SQL) and **online** (run against DB)
  modes – the online run uses a synchronous SQLAlchemy Engine pointing at the
  same PostgreSQL URL.  No async driver is required by Alembic.
"""
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

# ---------------------------------------------------------------------------
# 0. Alembic config & logging                                                 
# ---------------------------------------------------------------------------
config = context.config
if config.config_file_name:  # pragma: no cover
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

# ---------------------------------------------------------------------------
# 1.  Make sure project root is on PYTHONPATH                                 
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent  # hydroleaf/
sys.path.append(str(ROOT))

# ---------------------------------------------------------------------------
# 2.  Import the project's Base (single metadata source)                      
# ---------------------------------------------------------------------------
from app.core.database import Base  # noqa: E402  (after PYTHONPATH tweak)

# ---------------------------------------------------------------------------
# 3.  Auto‑import every model module so tables register with Base             
# ---------------------------------------------------------------------------

def _import_models() -> None:  # pragma: no cover – convenience helper
    """Import *app.models* (and its sub‑packages, if any) so Base is populated."""
    pkg = importlib.import_module("app.models")
    # If *app.models* is a single file the attribute __path__ is missing – in
    # that case there is nothing else to import.
    if getattr(pkg, "__path__", None):
        for _, mod_name, _ in iter_modules(pkg.__path__, pkg.__name__ + "."):
            importlib.import_module(mod_name)
    logger.debug("Imported model modules for Alembic autogenerate")

_import_models()

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# 4.  Inject DATABASE_URL into Alembic                                        
# ---------------------------------------------------------------------------

def _get_url() -> str:
    """Return a *sync‑driver* DB URL for Alembic.

    If the project uses `postgresql+asyncpg`, Alembic (which is sync) must
    connect with a synchronous driver such as `psycopg2`/`psycopg`.
    """
    env_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not env_url:
        raise RuntimeError("DATABASE_URL is not set for Alembic migrations")

    from sqlalchemy.engine.url import make_url

    url_obj = make_url(env_url)
    # Convert async driver → sync driver transparently for Alembic.
    if url_obj.drivername.endswith("+asyncpg"):
        url_obj = url_obj.set(drivername="postgresql+psycopg2")

    return str(url_obj)

# Push the resolved URL back into Alembic so `context.get_x_argument()` & co.
config.set_main_option("sqlalchemy.url", _get_url())

# ---------------------------------------------------------------------------
# 5.  Standard offline / online runners                                       
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Generate SQL scripts without hitting the database."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
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
