"""Access Tracker — SQLite-backed access log for vault notes.

Tracks every time a note is read, queried, or cited. Used by the
Retention Engine (Ebbinghaus decay) to compute reinforcement boost
at query time.

DB path: $XDG_CACHE_HOME/second-brain-engine/access.sqlite
       = ~/.cache/second-brain-engine/access.sqlite  (default)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def _default_access_db_path() -> Path:
    env_path = os.environ.get("ACCESS_DB_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    db_dir = cache / "second-brain-engine"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "access.sqlite"


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;

-- Tracks individual access events (append-only log)
CREATE TABLE IF NOT EXISTS access_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,           -- vault-relative path (e.g. "second-brain/Concepts/MCP.md")
    accessed_at REAL NOT NULL,           -- unix timestamp (time.time())
    source      TEXT NOT NULL DEFAULT 'read'  -- 'read', 'query', 'cite', 'batch_read'
);

CREATE INDEX IF NOT EXISTS idx_access_path ON access_events(path);
CREATE INDEX IF NOT EXISTS idx_access_time ON access_events(accessed_at);

-- Materialized summary per path (updated on each access)
CREATE TABLE IF NOT EXISTS access_summary (
    path            TEXT PRIMARY KEY,
    total_count     INTEGER NOT NULL DEFAULT 0,
    last_accessed   REAL NOT NULL DEFAULT 0,
    first_accessed  REAL NOT NULL DEFAULT 0
);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────

class AccessStats(NamedTuple):
    """Summary statistics for a single note's access history."""
    path: str
    total_count: int
    last_accessed: float   # unix timestamp
    first_accessed: float  # unix timestamp
    recent_timestamps: list[float]  # last N access timestamps (for Ebbinghaus boost)


# ──────────────────────────────────────────────────────────────────────────────
# AccessTracker
# ──────────────────────────────────────────────────────────────────────────────

class AccessTracker:
    """SQLite-backed access tracking for vault notes.

    Thread-safe via WAL mode. Designed for low-write, high-read workloads.

    Usage:
        tracker = AccessTracker()
        tracker.open()
        tracker.record("Concepts/MCP.md", source="read")
        stats = tracker.get_stats("Concepts/MCP.md")
        tracker.close()
    """

    # Max recent timestamps to keep for Ebbinghaus boost calculation
    MAX_RECENT = 20

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or _default_access_db_path()
        self._conn: sqlite3.Connection | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the DB and create schema if needed."""
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info(f"AccessTracker opened: {self._path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("AccessTracker not open. Call open() first.")
        return self._conn

    # ── Recording ─────────────────────────────────────────────────────────────

    def record(self, path: str, source: str = "read") -> None:
        """Record a single access event for a vault path.

        Args:
            path: Vault-relative path (e.g. "second-brain/Concepts/MCP.md")
            source: Type of access — 'read', 'query', 'cite', 'batch_read'
        """
        now = time.time()
        self.conn.execute(
            "INSERT INTO access_events (path, accessed_at, source) VALUES (?, ?, ?)",
            (path, now, source),
        )
        # Upsert the summary row
        self.conn.execute(
            """
            INSERT INTO access_summary (path, total_count, last_accessed, first_accessed)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                total_count = total_count + 1,
                last_accessed = MAX(last_accessed, excluded.last_accessed)
            """,
            (path, now, now),
        )
        self.conn.commit()

    def record_batch(self, paths: list[str], source: str = "batch_read") -> None:
        """Record access events for multiple paths in a single transaction."""
        now = time.time()
        with self.conn:
            for path in paths:
                self.conn.execute(
                    "INSERT INTO access_events (path, accessed_at, source) VALUES (?, ?, ?)",
                    (path, now, source),
                )
                self.conn.execute(
                    """
                    INSERT INTO access_summary (path, total_count, last_accessed, first_accessed)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        total_count = total_count + 1,
                        last_accessed = MAX(last_accessed, excluded.last_accessed)
                    """,
                    (path, now, now),
                )

    # ── Querying ──────────────────────────────────────────────────────────────

    def get_stats(self, path: str) -> AccessStats | None:
        """Get access statistics for a single vault path."""
        row = self.conn.execute(
            "SELECT * FROM access_summary WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None

        # Fetch recent timestamps for Ebbinghaus boost
        recent_rows = self.conn.execute(
            """
            SELECT accessed_at FROM access_events
            WHERE path = ?
            ORDER BY accessed_at DESC
            LIMIT ?
            """,
            (path, self.MAX_RECENT),
        ).fetchall()

        return AccessStats(
            path=row["path"],
            total_count=row["total_count"],
            last_accessed=row["last_accessed"],
            first_accessed=row["first_accessed"],
            recent_timestamps=[r["accessed_at"] for r in recent_rows],
        )

    def get_all_stats(self) -> list[AccessStats]:
        """Get access statistics for all tracked paths."""
        rows = self.conn.execute(
            "SELECT * FROM access_summary ORDER BY last_accessed DESC"
        ).fetchall()

        results = []
        for row in rows:
            recent_rows = self.conn.execute(
                """
                SELECT accessed_at FROM access_events
                WHERE path = ?
                ORDER BY accessed_at DESC
                LIMIT ?
                """,
                (row["path"], self.MAX_RECENT),
            ).fetchall()

            results.append(AccessStats(
                path=row["path"],
                total_count=row["total_count"],
                last_accessed=row["last_accessed"],
                first_accessed=row["first_accessed"],
                recent_timestamps=[r["accessed_at"] for r in recent_rows],
            ))
        return results

    def get_stale_paths(self, days: int = 30) -> list[AccessStats]:
        """Return paths not accessed in the last N days, sorted by staleness."""
        cutoff = time.time() - (days * 86400)
        rows = self.conn.execute(
            """
            SELECT * FROM access_summary
            WHERE last_accessed < ?
            ORDER BY last_accessed ASC
            """,
            (cutoff,),
        ).fetchall()

        return [
            AccessStats(
                path=row["path"],
                total_count=row["total_count"],
                last_accessed=row["last_accessed"],
                first_accessed=row["first_accessed"],
                recent_timestamps=[],  # no need for boost on stale queries
            )
            for row in rows
        ]

    # ── Maintenance ───────────────────────────────────────────────────────────

    def prune_old_events(self, keep_days: int = 90) -> int:
        """Delete individual access events older than N days.

        Keeps the summary table intact — only the granular event log
        is trimmed. Returns the number of rows deleted.
        """
        cutoff = time.time() - (keep_days * 86400)
        cur = self.conn.execute(
            "DELETE FROM access_events WHERE accessed_at < ?", (cutoff,)
        )
        self.conn.commit()
        deleted = cur.rowcount
        if deleted > 0:
            logger.info(f"Pruned {deleted} access events older than {keep_days} days")
        return deleted

    def stats(self) -> dict:
        """Return summary statistics about the access database."""
        event_count = self.conn.execute(
            "SELECT COUNT(*) FROM access_events"
        ).fetchone()[0]
        path_count = self.conn.execute(
            "SELECT COUNT(*) FROM access_summary"
        ).fetchone()[0]
        return {
            "tracked_paths": path_count,
            "total_events": event_count,
            "db_path": str(self._path),
        }
