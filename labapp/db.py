"""SQLite connection setup, schema creation / migrations, and transaction helper."""

import importlib.util
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
# Version 1 = schema.sql + seed_tests.sql. These two files are FROZEN: never edit
# them; every later change is a new numbered file in migrations/.
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
SEED_TESTS_PATH = PACKAGE_DIR / "seed_tests.sql"
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
_MIGRATION_FILE = re.compile(r"(\d{3})_[a-z0-9_]+\.(sql|py)")


def _find_migrations() -> list[tuple[int, Path]]:
    """[(version, path)] for migrations/NNN_name.sql|py, sorted. Versions start at 2."""
    found: dict[int, Path] = {}
    for path in MIGRATIONS_DIR.iterdir():
        m = _MIGRATION_FILE.fullmatch(path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version < 2 or version in found:
            raise RuntimeError(f"Bad or duplicate migration number: {path.name}")
        found[version] = path
    return sorted(found.items())


MIGRATIONS = _find_migrations()
SCHEMA_VERSION = max([1, *(v for v, _ in MIGRATIONS)])


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings every connection must have.

    autocommit=True: the sqlite3 module never opens transactions implicitly;
    we issue BEGIN IMMEDIATE / COMMIT ourselves (see `immediate`).
    """
    conn = sqlite3.connect(path, autocommit=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # foreign_keys is OFF by default and resets on every new connection.
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("This SQLite build does not support foreign keys")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")  # persistent in the file; cheap to repeat
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply(conn: sqlite3.Connection, version: int, run) -> None:
    """Run one schema step in its own transaction and stamp user_version."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _user_version(conn) >= version:  # another process got here first
            conn.execute("ROLLBACK")
            return
        run()
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _run_migration_file(conn: sqlite3.Connection, path: Path) -> None:
    if path.suffix == ".sql":
        # In autocommit=True mode executescript adds no COMMIT of its own,
        # so the script runs inside the transaction opened by _apply.
        conn.executescript(path.read_text(encoding="utf-8"))
    else:
        spec = importlib.util.spec_from_file_location(f"labapp_migration_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade(conn)


def _backup_before_upgrade(conn: sqlite3.Connection, version: int) -> Path | None:
    """Copy the database (consistent snapshot) before changing its schema."""
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not db_file:
        return None  # in-memory database
    target = Path(f"{db_file}.v{version}.bak")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def init_db(conn: sqlite3.Connection) -> None:
    """Create a new database or upgrade an existing one to SCHEMA_VERSION."""
    version = _user_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{version} is newer than this app (v{SCHEMA_VERSION})"
        )
    if version == 0:
        base = SCHEMA_PATH.read_text(encoding="utf-8") + "\n" + \
            SEED_TESTS_PATH.read_text(encoding="utf-8")
        _apply(conn, 1, lambda: conn.executescript(base))
    elif version < SCHEMA_VERSION:
        _backup_before_upgrade(conn, version)

    for v, path in MIGRATIONS:
        if v > _user_version(conn):
            _apply(conn, v, lambda p=path: _run_migration_file(conn, p))


@contextmanager
def immediate(conn: sqlite3.Connection):
    """Run a block inside BEGIN IMMEDIATE (takes the write lock up front)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
