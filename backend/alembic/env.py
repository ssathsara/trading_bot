"""Alembic environment. Uses DATABASE_URL from the environment / .env (never hardcoded)."""
import os
from logging.config import fileConfig

from alembic import context

from app.db import Base, make_engine
import app.models  # noqa: F401  (register models)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    from app.config import get_settings

    return get_settings().database_url


def render_item(type_, obj, autogen_context):
    if type_ == "type" and obj.__class__.__module__ == "app.db":
        autogen_context.imports.add("import app.db")
        return f"app.db.{obj.__class__.__name__}()"
    return False


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_item=render_item,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item,
                          render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
