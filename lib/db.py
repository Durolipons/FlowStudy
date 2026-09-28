"""Database connection, config, schema initialisation, and small helpers."""
from __future__ import annotations
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str | None = None) -> dict:
    if path is None:
        path = CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def get_conn(config: dict | None = None, db_path: str | None = None) -> sqlite3.Connection:
    """Return a configured SQLite connection. WAL, FK on, FTS-friendly PRAGMAs.

    If config['db_path'] is blank (or db_path is None and config omits it),
    defaults to <project>/data/library.db so the project is portable across
    machines and working directories.
    """
    if config is None:
        config = load_config()
    if db_path is None:
        db_path = config.get("db_path") or os.path.join(BASE_DIR, "data", "library.db")
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA mmap_size=30000000000;")          # ~30 GB mmap
    conn.execute("PRAGMA cache_size=-200000;")             # ~200 MB page cache / dir
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA case_sensitive_like=OFF;")
    try:
        conn.execute("PRAGMA trusted_schema=ON;")
    except sqlite3.OperationalError:
        pass  # very new pragma, harmless if absent
    return conn


@contextmanager
def connect(config: dict | None = None, db_path: str | None = None):
    conn = get_conn(config, db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables / triggers from schema.sql.

    Idempotent: harmless when the schema is already applied. Detects this by
    trying a no-op CREATE for the meta table; if meta exists we assume the
    full schema was applied by a previous run. Use init_db.py --reset for a
    clean slate instead of dropping live tables here.
    """
    version = schema_version(conn)
    if version != "none":
        return
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        return row["v"] if row else "none"
    except sqlite3.OperationalError:
        return "none"


# ---------------------------------------------------------------------------
# Tiny utilities shared by scanners / normaliser
# ---------------------------------------------------------------------------
ISO8601_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}")


def now() -> str:
    return sqlite3.connect(":memory:").execute("SELECT datetime('now')") \
        .fetchone()[0]


def insert_or_get(cur: sqlite3.Cursor, table: str, unique_cols: Iterable[str],
                  values: dict) -> int:
    """Insert if absent, return id. unique_cols must be a UNIQUE constraint."""
    uc = list(unique_cols)
    sel = "SELECT id FROM %s WHERE %s" % (table, " AND ".join(
        "%s = ?" % c for c in uc))
    params = [values[c] for c in uc]
    row = cur.execute(sel, params).fetchone()
    if row:
        return row["id"]
    cols = list(values.keys())
    ph = ",".join(["?"] * len(cols))
    col = ",".join(cols)
    cur.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, col, ph),
                [values[c] for c in cols])
    return cur.lastrowid


def long_path(path: str) -> str:
    """Return a \\?\\-prefixed path for >260-char safety (idempotent)."""
    if path.startswith("\\\\?\\"):
        return path
    abs_p = os.path.abspath(path)
    # \\?\ prefix; also normalise backslashes
    return "\\\\?\\" + abs_p
