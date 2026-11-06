import io
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, redirect_stdout
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from filelock import FileLock

from social_mcp import database
from social_mcp.accounts import save_account
from social_mcp.database import initialize_database

ROOT = Path(__file__).resolve().parents[1]


def query(path, sql):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()


def test_initial_migration_is_repeatable_and_stdout_is_clean(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    output = io.StringIO()
    with redirect_stdout(output):
        initialize_database(path)
        initialize_database(path)
    assert query(path, "SELECT version_num FROM alembic_version") == [
        ("0002_publications",)
    ]
    assert output.getvalue() == ""


def copy_migrations(tmp_path, monkeypatch, script):
    target = tmp_path / "migration-scripts"
    shutil.copytree(
        database.MIGRATIONS_PATH, target, ignore=shutil.ignore_patterns("__pycache__")
    )
    (target / "versions/0002_test.py").write_text(script, encoding="utf-8")
    monkeypatch.setattr("social_mcp.database.MIGRATIONS_PATH", target)


def test_next_batch_migration_preserves_data(db_path, tmp_path, monkeypatch):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token="synthetic-batch-token",
        obtained_at=100,
        expires_at=None,
    )
    before = query(db_path, "SELECT * FROM accounts")
    copy_migrations(
        tmp_path,
        monkeypatch,
        """
from alembic import op
import sqlalchemy as sa
revision = "0002_test"
down_revision = "0002_publications"
def upgrade():
    with op.batch_alter_table("accounts", recreate="always") as batch:
        batch.add_column(sa.Column("scopes", sa.Text(), server_default="read"))
def downgrade():
    pass
""",
    )
    initialize_database(db_path)
    initialize_database(db_path)
    assert [row[:6] for row in query(db_path, "SELECT * FROM accounts")] == before
    assert query(db_path, "SELECT scopes FROM accounts") == [("read",)]
    assert query(db_path, "SELECT version_num FROM alembic_version") == [("0002_test",)]


def test_failed_migration_rolls_back_schema_and_version(db_path, tmp_path, monkeypatch):
    copy_migrations(
        tmp_path,
        monkeypatch,
        """
from alembic import op
import sqlalchemy as sa
revision = "0002_test"
down_revision = "0002_publications"
def upgrade():
    op.add_column("accounts", sa.Column("unfinished", sa.Text()))
    raise RuntimeError("synthetic migration failure")
def downgrade():
    pass
""",
    )
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        initialize_database(db_path)
    assert "unfinished" not in {
        row[1] for row in query(db_path, "PRAGMA table_info(accounts)")
    }
    assert query(db_path, "SELECT version_num FROM alembic_version") == [
        ("0002_publications",)
    ]


def test_newer_database_is_not_downgraded(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE alembic_version SET version_num='future_version'"
            )
    with pytest.raises(CommandError):
        initialize_database(db_path)
    assert query(db_path, "SELECT version_num FROM alembic_version") == [
        ("future_version",)
    ]


def test_startup_waits_for_other_process_migration_lock(tmp_path):
    path = tmp_path / "locked.sqlite3"
    ready = tmp_path / "ready"
    script = """
import sys
from pathlib import Path
from social_mcp.database import initialize_database
Path(sys.argv[2]).touch()
initialize_database(Path(sys.argv[1]))
"""
    with FileLock(f"{path}-migrate.lock", timeout=5):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert process.poll() is None, "Migration worker exited before ready"
                assert time.monotonic() < deadline, "Migration worker did not start"
                time.sleep(0.01)
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.4)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr.decode()
    assert stdout == b""
    assert query(path, "SELECT version_num FROM alembic_version") == [
        ("0002_publications",)
    ]


def test_parallel_initialization(tmp_path):
    path = tmp_path / "parallel.sqlite3"
    script = "from pathlib import Path; import sys; from social_mcp.database import initialize_database; initialize_database(Path(sys.argv[1]))"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode()
            assert stdout == b""
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert query(path, "SELECT version_num FROM alembic_version") == [
        ("0002_publications",)
    ]


def test_alembic_metadata_matches_current_schema(db_path):
    result = subprocess.run(
        [str(Path(sys.executable).parent / "alembic"), "check"],
        cwd=ROOT,
        env={**os.environ, "SOCIAL_MCP_DB_PATH": str(db_path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_publication_migration_on_copy_preserves_original_accounts(tmp_path):
    from alembic import command
    from alembic.config import Config

    from social_mcp.accounts import get_account
    from social_mcp.database import migration_connection
    from social_mcp.threads.publications import PublicationStore

    original = tmp_path / "previous.sqlite3"
    upgraded = tmp_path / "upgraded-copy.sqlite3"
    with migration_connection(original) as connection:
        config = Config(attributes={"connection": connection})
        config.set_main_option("script_location", str(database.MIGRATIONS_PATH))
        command.upgrade(config, "0001_accounts")
    save_account(
        original,
        platform="threads",
        account_id="123",
        username="existing",
        access_token="synthetic-preserved-token",
        obtained_at=100,
        expires_at=4000000000,
    )
    before = query(original, "SELECT * FROM accounts")
    with (
        closing(sqlite3.connect(original)) as source,
        closing(sqlite3.connect(upgraded)) as target,
    ):
        source.backup(target)
    initialize_database(upgraded)
    assert query(upgraded, "SELECT * FROM accounts") == before
    assert query(original, "SELECT version_num FROM alembic_version") == [
        ("0001_accounts",)
    ]
    assert query(upgraded, "SELECT version_num FROM alembic_version") == [
        ("0002_publications",)
    ]
    assert (
        get_account(
            upgraded, platform="threads", account_id="123"
        ).access_token.get_secret_value()
        == "synthetic-preserved-token"
    )
    store = PublicationStore(upgraded)
    publication_id = store.create("123", container_id="456")
    assert store.get("123", publication_id).state == "preparing"
