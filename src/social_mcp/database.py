"""SQLite connections and sequential migration execution."""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from filelock import FileLock
from sqlalchemy import URL, Connection, Engine, create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

MIGRATIONS_PATH = Path(__file__).with_name("migrations")


class Base(DeclarativeBase):
    pass


@contextmanager
def open_database(path: Path, *, mode: Literal["ro", "rw"] = "rw") -> Iterator[Engine]:
    """Open an existing database; never create a file for an incorrect path."""
    url = URL.create(
        "sqlite+pysqlite",
        database=path.resolve().as_uri(),
        query={"mode": mode, "uri": "true"},
    )
    engine = create_engine(
        url,
        connect_args={"autocommit": False, "timeout": 10},
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@contextmanager
def migration_connection(path: Path) -> Iterator[Connection]:
    """Create a protected file and serialize migrations across processes."""
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    if os.name == "posix":
        path.chmod(0o600)

    with FileLock(f"{path}-migrate.lock", timeout=30, mode=0o600):
        with open_database(path) as engine, engine.begin() as connection:
            yield connection


def initialize_database(path: Path) -> None:
    """Upgrade a new or existing database to the current package revision."""
    with migration_connection(path) as connection:
        config = Config(stdout=sys.stderr, attributes={"connection": connection})
        config.set_main_option(
            "script_location", str(MIGRATIONS_PATH).replace("%", "%%")
        )
        command.upgrade(config, "head")
