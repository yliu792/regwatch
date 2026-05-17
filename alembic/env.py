"""
Alembic environment configuration for async PostgreSQL.

Reads DATABASE_URL from the environment (via python-dotenv) and uses
``app.models.Base.metadata`` as the autogenerate target.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# ---------------------------------------------------------------------------
# Load env & set up Alembic config
# ---------------------------------------------------------------------------
load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow env.py to use the URL from the .ini as a fallback, but prefer
# the DATABASE_URL environment variable.
DATABASE_URL = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))

# ---------------------------------------------------------------------------
# Model metadata for autogenerate
# ---------------------------------------------------------------------------
from app.models import Base  # noqa: E402

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode (URL only, no engine)
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits DDL to a script — no DB connection needed.
    """
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode (async engine)
# ---------------------------------------------------------------------------
def do_run_migrations(connection):
    """Execute migrations within a synchronous connection context."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Create an async engine and run migrations via ``run_sync``."""
    connectable = create_async_engine(
        DATABASE_URL,
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
