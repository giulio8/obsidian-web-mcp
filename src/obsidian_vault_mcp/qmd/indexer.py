"""Vault indexer for QMD-Lite.

Two modes:
  - Full index:        scan all .md files in the vault, index everything
  - Incremental index: re-index only files whose content has changed

Embedding is done in batches for efficiency (fewer API calls to Vertex AI).

Typical call:
    indexer = VaultIndexer(vault_path, db)
    stats = indexer.run_full()   # first-time setup
    stats = indexer.run_delta()  # fast incremental update

Both methods return an IndexStats dataclass with counts.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import Chunk, chunk_file
from .db import QMDDatabase
from .vertex_client import embed_texts

logger = logging.getLogger(__name__)

# File types to index
INDEXED_EXTENSIONS = {".md"}

# Directories to skip (mirrors vault.py EXCLUDED_DIRS)
EXCLUDED_DIRS = {".obsidian", ".trash", ".git", ".DS_Store", "__pycache__"}

# How many chunks to embed in a single Vertex AI call
EMBED_BATCH_SIZE = 100


@dataclass
class IndexStats:
    indexed: int = 0      # files newly indexed
    updated: int = 0      # files re-indexed (content changed)
    unchanged: int = 0    # files skipped (content identical)
    removed: int = 0      # DB records removed (file deleted)
    chunks_added: int = 0
    errors: int = 0
    elapsed_sec: float = 0.0


class VaultIndexer:
    """Index markdown files from a vault directory into QMDDatabase."""

    def __init__(self, vault_path: Path, db: QMDDatabase, embed: bool = True):
        """
        Args:
            vault_path: root of the Obsidian vault (rclone mount)
            db:         open QMDDatabase instance
            embed:      if False, skip embedding (useful for fast BM25-only mode)
        """
        self.vault_path = vault_path
        self.db = db
        self.embed = embed

    # ── Public API ────────────────────────────────────────────────────────────

    def run_full(self) -> IndexStats:
        """Index all eligible files. Slower but thorough."""
        return self._run(force=True)

    def run_delta(self) -> IndexStats:
        """Index only files that are new or modified."""
        return self._run(force=False)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, force: bool) -> IndexStats:
        start = time.time()
        stats = IndexStats()

        md_files = list(self._iter_markdown_files())
        logger.info(
            f"Indexer: found {len(md_files)} markdown files in {self.vault_path}"
        )

        # Track which paths are on disk to detect deletions
        disk_paths = {str(p.relative_to(self.vault_path)) for p in md_files}

        # --- Deletions ---
        for row in self.db.conn.execute("SELECT id, path FROM documents").fetchall():
            if row["path"] not in disk_paths:
                self.db.delete_document_chunks(row["id"])
                self.db.conn.execute(
                    "DELETE FROM documents WHERE id = ?", (row["id"],)
                )
                stats.removed += 1
                logger.debug(f"Removed stale document: {row['path']}")

        # --- Index / update ---
        pending_chunks: list[Chunk] = []    # chunks waiting for embedding
        pending_doc_ids: list[int] = []     # parallel list of doc_ids

        def flush_embeddings():
            if not pending_chunks:
                return
            texts = [c.text for c in pending_chunks]
            embeddings = embed_texts(texts) if self.embed else [[]] * len(texts)
            for chunk, doc_id, emb in zip(pending_chunks, pending_doc_ids, embeddings):
                chunk_id = self.db.insert_chunk(
                    doc_id=doc_id,
                    chunk_index=chunk.chunk_index,
                    header_path=chunk.header_path,
                    char_offset=chunk.char_offset,
                    text=chunk.text,
                )
                self.db.insert_vector(chunk_id, emb)
                stats.chunks_added += 1
            pending_chunks.clear()
            pending_doc_ids.clear()
            self.db.commit()
            logger.debug(f"Flushed batch: {stats.chunks_added} chunks so far")

        for md_path in md_files:
            rel_path = str(md_path.relative_to(self.vault_path))
            try:
                content = md_path.read_text(encoding="utf-8", errors="replace")
                mtime = md_path.stat().st_mtime

                if not force and not self.db.needs_reindex(rel_path, content):
                    stats.unchanged += 1
                    continue

                is_new = self.db.get_document(rel_path) is None

                # Parse chunks BEFORE upserting so we have the title
                chunks = chunk_file(rel_path, content)
                title = chunks[0].doc_title if chunks else md_path.stem

                doc_id = self.db.upsert_document(rel_path, content, title, mtime)

                # Delete old chunks for this document
                self.db.delete_document_chunks(doc_id)

                # Queue new chunks
                for chunk in chunks:
                    pending_chunks.append(chunk)
                    pending_doc_ids.append(doc_id)

                    if len(pending_chunks) >= EMBED_BATCH_SIZE:
                        flush_embeddings()

                if is_new:
                    stats.indexed += 1
                else:
                    stats.updated += 1

                logger.debug(
                    f"{'New' if is_new else 'Updated'}: {rel_path} "
                    f"({len(chunks)} chunks)"
                )

            except Exception as e:
                logger.error(f"Failed to index {rel_path}: {e}")
                stats.errors += 1

        # Flush remaining
        flush_embeddings()

        stats.elapsed_sec = time.time() - start
        logger.info(
            f"Indexing complete in {stats.elapsed_sec:.1f}s — "
            f"indexed={stats.indexed}, updated={stats.updated}, "
            f"unchanged={stats.unchanged}, removed={stats.removed}, "
            f"chunks={stats.chunks_added}, errors={stats.errors}"
        )
        return stats

    def _iter_markdown_files(self):
        """Yield all .md files in the vault, skipping excluded directories."""
        for path in self.vault_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in INDEXED_EXTENSIONS:
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            yield path
