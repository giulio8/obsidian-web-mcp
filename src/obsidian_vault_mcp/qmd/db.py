"""SQLite database layer for QMD-Lite.

Schema:
  documents      - one row per .md file (path, content hash, title, mtime)
  chunks         - one row per chunk (fk to documents, text, header_path, offset)
  chunks_fts     - FTS5 virtual table for BM25 full-text search
  chunk_vectors  - float32 blobs for sqlite-vec cosine similarity search

sqlite-vec must be installed as a loadable extension.
On Debian/Ubuntu: pip install sqlite-vec (provides the .so automatically)

DB path: $XDG_CACHE_HOME/qmd-lite/index.sqlite
       = ~/.cache/qmd-lite/index.sqlite  (default)
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    env_path = os.environ.get("VAULT_DB_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    db_dir = cache / "qmd-lite"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "index.sqlite"


# ──────────────────────────────────────────────────────────────────────────────
# sqlite-vec helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension. Returns True on success."""
    conn.enable_load_extension(True)
    try:
        import sqlite_vec  # type: ignore
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as e:
        logger.warning(f"sqlite-vec not available: {e}. Vector search disabled.")
        conn.enable_load_extension(False)
        return False


def _serialize_vector(v: list[float]) -> bytes:
    """Pack a float list into a little-endian float32 blob."""
    return struct.pack(f"{len(v)}f", *v)


def _deserialize_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,   -- vault-relative path
    content_hash TEXT NOT NULL,         -- sha256 of file content
    title       TEXT NOT NULL DEFAULT '',
    mtime       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    header_path  TEXT NOT NULL DEFAULT '',
    char_offset  INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    doc_path     TEXT NOT NULL DEFAULT ''  -- denormalized from documents.path for O(1) prefix filter
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_path ON chunks(doc_path);

-- FTS5 index for BM25 keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    header_path,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, header_path)
    VALUES (new.id, new.text, new.header_path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, header_path)
    VALUES ('delete', old.id, old.text, old.header_path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, header_path)
    VALUES ('delete', old.id, old.text, old.header_path);
    INSERT INTO chunks_fts(rowid, text, header_path)
    VALUES (new.id, new.text, new.header_path);
END;

-- Vector table (created only if sqlite-vec is available)
-- Defined via sqlite-vec API, not standard DDL
"""

_VEC_TABLE_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[{dim}]
);
"""


# ──────────────────────────────────────────────────────────────────────────────
# QMDDatabase
# ──────────────────────────────────────────────────────────────────────────────

class QMDDatabase:
    """SQLite-backed store for chunks, FTS5 index, and vector embeddings."""

    def __init__(self, db_path: Path | None = None, embed_dim: int = 768):
        self._path = db_path or _default_db_path()
        self._embed_dim = embed_dim
        self._conn: sqlite3.Connection | None = None
        self._vec_enabled = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the DB and create schema if needed."""
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        self._vec_enabled = _load_vec_extension(self._conn)

        # Apply base schema
        self._conn.executescript(_SCHEMA)

        # Migration: add doc_path column to chunks if not present (one-time, idempotent)
        self._migrate_add_doc_path()

        # Create vector table if extension loaded
        if self._vec_enabled:
            try:
                self._conn.execute(
                    _VEC_TABLE_DDL.format(dim=self._embed_dim)
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # table already exists

        self._conn.commit()
        logger.info(
            f"QMDDatabase opened: {self._path} "
            f"(vec={'enabled' if self._vec_enabled else 'disabled'})"
        )

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
            raise RuntimeError("Database not open. Call open() first.")
        return self._conn

    # ── Document tracking ─────────────────────────────────────────────────────

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def get_document(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE path = ?", (path,)
        ).fetchone()

    def needs_reindex(self, path: str, content: str) -> bool:
        """Return True if the file is new or its content changed."""
        row = self.get_document(path)
        if row is None:
            return True
        return row["content_hash"] != self.content_hash(content)

    def upsert_document(self, path: str, content: str, title: str, mtime: float) -> int:
        """Insert or update document record. Returns doc_id."""
        h = self.content_hash(content)
        self.conn.execute(
            """
            INSERT INTO documents (path, content_hash, title, mtime)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                content_hash = excluded.content_hash,
                title = excluded.title,
                mtime = excluded.mtime
            """,
            (path, h, title, mtime),
        )
        row = self.conn.execute(
            "SELECT id FROM documents WHERE path = ?", (path,)
        ).fetchone()
        return row["id"]

    def delete_document_chunks(self, doc_id: int) -> None:
        """Delete all chunks (and their vectors) for a document."""
        if self._vec_enabled:
            chunk_ids = [
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)
                )
            ]
            for cid in chunk_ids:
                self.conn.execute(
                    "DELETE FROM chunk_vectors WHERE chunk_id = ?", (cid,)
                )
        self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    # ── Chunk storage ─────────────────────────────────────────────────────────

    def _migrate_add_doc_path(self) -> None:
        """Idempotent migration: add doc_path column + index to chunks if absent."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(chunks)")}
        if "doc_path" not in cols:
            logger.info("Migration: adding doc_path column to chunks table")
            self.conn.execute("ALTER TABLE chunks ADD COLUMN doc_path TEXT NOT NULL DEFAULT ''")
            # Backfill from documents table
            self.conn.execute(
                """
                UPDATE chunks SET doc_path = (
                    SELECT path FROM documents WHERE documents.id = chunks.doc_id
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_path ON chunks(doc_path)")
            self.conn.commit()
            logger.info("Migration complete: doc_path backfilled and indexed")

    def insert_chunk(
        self,
        doc_id: int,
        chunk_index: int,
        header_path: str,
        char_offset: int,
        text: str,
        doc_path: str = "",
    ) -> int:
        """Insert a chunk row. Returns chunk_id."""
        cur = self.conn.execute(
            """
            INSERT INTO chunks (doc_id, chunk_index, header_path, char_offset, text, doc_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, chunk_index, header_path, char_offset, text, doc_path),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def insert_vector(self, chunk_id: int, embedding: list[float]) -> None:
        """Store vector for a chunk. No-op if sqlite-vec is unavailable."""
        if not self._vec_enabled or not embedding:
            return
        blob = _serialize_vector(embedding)
        self.conn.execute(
            "INSERT OR REPLACE INTO chunk_vectors(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, blob),
        )

    def commit(self) -> None:
        self.conn.commit()

    def get_first_chunks_by_paths(self, paths: list[str]) -> list[dict]:
        """Fetch the first chunk (chunk_index=0) for multiple documents.
        
        Used to materialize graph search results.
        Returns a list of dicts similar to bm25_search but without scores.
        """
        if not paths:
            return []
            
        placeholders = ",".join("?" * len(paths))
        query = f"""
            SELECT
                c.id              AS chunk_id,
                d.path            AS doc_path,
                d.title           AS doc_title,
                c.text            AS text,
                c.header_path     AS header_path,
                c.char_offset     AS char_offset
            FROM documents d
            JOIN chunks c ON c.doc_id = d.id
            WHERE d.path IN ({placeholders}) AND c.chunk_index = 0
        """
        rows = self.conn.execute(query, paths).fetchall()
        return [dict(r) for r in rows]

    # ── Search ────────────────────────────────────────────────────────────────

    def bm25_search(
        self, query: str, limit: int = 20
    ) -> list[dict]:
        """BM25 keyword search using FTS5.

        Returns list of dicts with keys: chunk_id, doc_path, text,
        header_path, doc_title, score (positive, higher = better).
        """
        rows = self.conn.execute(
            """
            SELECT
                c.id              AS chunk_id,
                d.path            AS doc_path,
                d.title           AS doc_title,
                c.text            AS text,
                c.header_path     AS header_path,
                c.char_offset     AS char_offset,
                -bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN documents d ON d.id = c.doc_id
            WHERE chunks_fts MATCH ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def bm25_search_prefix(
        self, query: str, path_prefix: str, limit: int = 20
    ) -> list[dict]:
        """BM25 keyword search restricted to a vault path prefix.

        Uses the denormalized doc_path column on chunks for an efficient
        pre-filter before BM25 scoring — same pattern as Pinecone/Qdrant
        metadata pre-filtering.
        """
        rows = self.conn.execute(
            """
            SELECT
                c.id              AS chunk_id,
                d.path            AS doc_path,
                d.title           AS doc_title,
                c.text            AS text,
                c.header_path     AS header_path,
                c.char_offset     AS char_offset,
                -bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN documents d ON d.id = c.doc_id
            WHERE chunks_fts MATCH ?
              AND c.doc_path LIKE ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (query, path_prefix.rstrip("/") + "/%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def vector_search_prefix(
        self,
        embedding: list[float],
        path_prefix: str,
        limit: int = 20,
    ) -> list[dict]:
        """Cosine similarity search restricted to a vault path prefix.

        Implements the production-grade pre-filter + in-memory KNN pattern
        (Qdrant payload filter, MongoDB Atlas pre-filter, Pinecone metadata
        filter). Steps:
          1. Fetch all (chunk_id, embedding) blobs for the prefix subset — O(n_subset)
          2. Compute cosine similarity in Python/struct — trivial for typical subset sizes
             (e.g. Timeline/2026/06/ ≈ 100 chunks × 768 dims = ~300KB)
          3. Return top-k sorted by score

        Falls back to empty list if sqlite-vec is unavailable or subset is empty.
        """
        if not self._vec_enabled or not embedding:
            return []

        import struct as _struct
        import math as _math

        prefix_pattern = path_prefix.rstrip("/") + "/%"

        # Step 1: fetch chunk_ids + raw embedding blobs for the prefix subset
        rows = self.conn.execute(
            """
            SELECT
                cv.chunk_id,
                cv.embedding,
                d.path    AS doc_path,
                d.title   AS doc_title,
                c.text    AS text,
                c.header_path AS header_path,
                c.char_offset AS char_offset
            FROM chunk_vectors cv
            JOIN chunks c ON c.id = cv.chunk_id
            JOIN documents d ON d.id = c.doc_id
            WHERE c.doc_path LIKE ?
            """,
            (prefix_pattern,),
        ).fetchall()

        if not rows:
            return []

        # Step 2: compute cosine similarity in Python
        n = len(embedding)
        q_norm = _math.sqrt(sum(x * x for x in embedding))
        if q_norm == 0:
            return []

        scored: list[tuple[float, dict]] = []
        for row in rows:
            blob = row["embedding"]
            vec = list(_struct.unpack(f"{len(blob) // 4}f", blob))
            dot = sum(a * b for a, b in zip(embedding, vec))
            v_norm = _math.sqrt(sum(x * x for x in vec))
            cosine = dot / (q_norm * v_norm) if v_norm > 0 else 0.0
            # Convert to 0-1 score (cosine is already in [-1, 1]; notes are positive)
            score = (cosine + 1.0) / 2.0
            scored.append((score, dict(row)))

        # Step 3: top-k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {**item, "score": score}
            for score, item in scored[:limit]
        ]

    def vector_search(
        self, embedding: list[float], limit: int = 20
    ) -> list[dict]:
        """Cosine similarity search using sqlite-vec.

        Returns list of dicts with keys: chunk_id, doc_path, text,
        header_path, doc_title, score (0-1, higher = better).
        Falls back to empty list if sqlite-vec is unavailable.
        """
        if not self._vec_enabled or not embedding:
            return []

        blob = _serialize_vector(embedding)
        try:
            rows = self.conn.execute(
                """
                SELECT
                    cv.chunk_id,
                    d.path        AS doc_path,
                    d.title       AS doc_title,
                    c.text        AS text,
                    c.header_path AS header_path,
                    c.char_offset AS char_offset,
                    cv.distance   AS distance
                FROM chunk_vectors cv
                JOIN chunks c ON c.id = cv.chunk_id
                JOIN documents d ON d.id = c.doc_id
                WHERE cv.embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (blob, limit),
            ).fetchall()
            return [
                {**dict(r), "score": 1.0 / (1.0 + r["distance"])}
                for r in rows
            ]
        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            return []

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        doc_count = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunk_count = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vec_count = 0
        if self._vec_enabled:
            try:
                vec_count = self.conn.execute(
                    "SELECT COUNT(*) FROM chunk_vectors"
                ).fetchone()[0]
            except Exception:
                pass
        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "vectors": vec_count,
            "vec_enabled": self._vec_enabled,
            "db_path": str(self._path),
        }
