"""Forwarder state: per-file byte offsets for resume-after-restart.

Stored in SQLite. One row per watched file. Writes are synchronous so a
crash leaves us with the last committed offset, not a partial one.

Schema is versioned. Each version has an idempotent migration step. Upgrades
from prior scaffold DBs succeed without manual intervention."""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path


SCHEMA_VERSION = 2


def _norm(path: str) -> str:
    """Normalize path separators so Windows and Unix lookups match writes."""
    return path.replace("\\", "/")


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent migrations up to SCHEMA_VERSION."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    current = int(row[0]) if row else 0

    if current < 1:
        # v1: base tables.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_offsets (
                file_path TEXT PRIMARY KEY,
                file_id TEXT,
                byte_offset INTEGER NOT NULL,
                last_sha256 TEXT,
                last_flushed_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS db_cursors (
                db_path TEXT PRIMARY KEY,
                table_name TEXT NOT NULL,
                last_row_id INTEGER NOT NULL,
                last_flushed_ns INTEGER NOT NULL
            );
        """)

    if current < 2:
        # v2: add last_line_number to file_offsets. Idempotent because we
        # check PRAGMA table_info first — covers both fresh creates (column
        # from v1 block didn't include it) and in-place upgrades.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(file_offsets)")}
        if "last_line_number" not in cols:
            conn.execute(
                "ALTER TABLE file_offsets ADD COLUMN last_line_number INTEGER NOT NULL DEFAULT 0"
            )

    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        _migrate(self._conn)

    def get_offset(self, file_path: str) -> tuple[int, int]:
        """Return (byte_offset, last_line_number) for a file, or (0, 0) if unknown."""
        row = self._conn.execute(
            "SELECT byte_offset, last_line_number FROM file_offsets WHERE file_path = ?",
            (_norm(file_path),),
        ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def set_offset(self, file_path: str, byte_offset: int, line_number: int, sha256: str,
                   flushed_ns: int, file_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_offsets (file_path, file_id, byte_offset, last_line_number,
                                             last_sha256, last_flushed_ns)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     file_id = excluded.file_id,
                     byte_offset = excluded.byte_offset,
                     last_line_number = excluded.last_line_number,
                     last_sha256 = excluded.last_sha256,
                     last_flushed_ns = excluded.last_flushed_ns""",
                (_norm(file_path), file_id, byte_offset, line_number, sha256, flushed_ns),
            )

    def get_cursor(self, db_path: str) -> int:
        row = self._conn.execute(
            "SELECT last_row_id FROM db_cursors WHERE db_path = ?", (_norm(db_path),)
        ).fetchone()
        return row[0] if row else 0

    def set_cursor(self, db_path: str, table_name: str, row_id: int, flushed_ns: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO db_cursors (db_path, table_name, last_row_id, last_flushed_ns)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(db_path) DO UPDATE SET
                     last_row_id = excluded.last_row_id,
                     last_flushed_ns = excluded.last_flushed_ns""",
                (_norm(db_path), table_name, row_id, flushed_ns),
            )

    def close(self) -> None:
        self._conn.close()
