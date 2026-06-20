"""Tests for path scoping, prefix routing, and DB migration logic."""

import pytest
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

from obsidian_vault_mcp.qmd.db import QMDDatabase
from obsidian_vault_mcp.qmd.indexer import VaultIndexer
from obsidian_vault_mcp.qmd.search_engine import HybridSearchEngine, SubQuery
from obsidian_vault_mcp.server import vault_search, VaultSearchInput


def test_db_migration_idx_bootstrap(tmp_path):
    """Test that QMDDatabase handles opening a new/existing DB, runs the migration,
    and dynamically creates the doc_path index without bootstrap errors.
    """
    db_path = tmp_path / "test_qmd.sqlite"
    
    # 1. Open new database (fresh schema)
    with QMDDatabase(db_path=db_path) as db:
        # Check table columns
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(chunks)").fetchall()}
        assert "doc_path" in cols
        
        # Check index exists
        indices = {row[1] for row in db.conn.execute("PRAGMA index_list(chunks)").fetchall()}
        assert "idx_chunks_doc_path" in indices

    # 2. Simulate opening an OLD database that does NOT have doc_path column or index
    # We remove the database file and recreate it with the old schema manually
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db_path))
    # Old chunks schema (no doc_path column)
    conn.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            header_path TEXT NOT NULL DEFAULT '',
            char_offset INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    # Now open it via QMDDatabase and verify migration successfully adds doc_path and creates the index
    with QMDDatabase(db_path=db_path) as db:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(chunks)").fetchall()}
        assert "doc_path" in cols
        
        indices = {row[1] for row in db.conn.execute("PRAGMA index_list(chunks)").fetchall()}
        assert "idx_chunks_doc_path" in indices


def test_indexer_batch_doc_path(tmp_path):
    """Test that the indexer assigns the correct individual document path to each chunk
    during batch indexing, avoiding Python closure scope bugs.
    """
    db_path = tmp_path / "test_indexer.sqlite"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    # Create two files
    file1 = vault_path / "note1.md"
    file1.write_text("# Note 1\n\nThis is text for note 1.")
    
    file2 = vault_path / "note2.md"
    file2.write_text("# Note 2\n\nThis is text for note 2.")

    with QMDDatabase(db_path=db_path) as db:
        # Run indexer
        indexer = VaultIndexer(vault_path=vault_path, db=db, embed=False)
        # Mock embed_texts to return empty/dummy lists
        with patch("obsidian_vault_mcp.qmd.indexer.embed_texts", return_value=[[]]*10):
            # Temporarily set batch size to 1 to force flushes
            with patch("obsidian_vault_mcp.qmd.indexer.EMBED_BATCH_SIZE", 1):
                indexer.run_full()

        # Query chunks and verify doc_path values match their corresponding document
        rows = db.conn.execute(
            "SELECT c.doc_path, d.path FROM chunks c JOIN documents d ON d.id = c.doc_id"
        ).fetchall()
        
        assert len(rows) == 2
        for row in rows:
            # The chunk's doc_path column must exactly match the document path
            assert row["doc_path"] == row["path"]


def test_search_engine_primary_path_prefix(tmp_path):
    """Test that HybridSearchEngine.search forwards the primary path_prefix
    parameter to the primary search target.
    """
    db_path = tmp_path / "test_search.sqlite"
    with QMDDatabase(db_path=db_path) as db:
        engine = HybridSearchEngine(db)
        
        # Mock the internal search backends
        engine._safe_bm25 = MagicMock(return_value=[])
        
        engine.search(
            query="test query",
            path_prefix="second-brain/Timeline/2026/06/",
            embed_fn=None,
        )
        
        # Verify the primary query was called with the primary prefix
        engine._safe_bm25.assert_called_with("test query", 30, path_prefix="second-brain/Timeline/2026/06/")


@patch("obsidian_vault_mcp.qmd.vertex_client.route_query")
@patch("obsidian_vault_mcp.server.frontmatter_index")
@patch("obsidian_vault_mcp.qmd.db.QMDDatabase")
@patch("obsidian_vault_mcp.qmd.search_engine.HybridSearchEngine")
def test_server_route_normalization(mock_engine_cls, mock_db_cls, mock_frontmatter, mock_route_query):
    """Test that server.py vault_search normalizes logical prefixes returned by route_query."""
    # Set up mock search engine
    mock_engine = MagicMock()
    mock_engine_cls.return_value = mock_engine
    
    # Set up mock database stats
    mock_db = MagicMock()
    mock_db.stats.return_value = {"chunks": 100}
    mock_db_cls.return_value.__enter__.return_value = mock_db
    
    # Mock route_query to return a sub-query with un-normalized prefix
    mock_route_query.return_value = [
        SubQuery(query="sub query", path_prefix="Timeline/2026/06/", weight=1.0)
    ]
    
    with patch("obsidian_vault_mcp.config.VAULT_RCLONE_PREFIX", "second-brain"):
        # Execute search with expand=True
        # We also pass a user-defined path_prefix which should be normalized too
        vault_search(
            query="test query",
            semantic=True,
            expand=True,
            path_prefix="Editors/Giulio/",
        )
        
        # 1. Verify search was called on the engine
        mock_engine.search.assert_called_once()
        kwargs = mock_engine.search.call_args[1]
        
        # 2. Verify sub_queries path_prefix was normalized (second-brain/ prepended)
        sub_queries = kwargs["sub_queries"]
        assert len(sub_queries) == 1
        assert sub_queries[0].path_prefix == "second-brain/Timeline/2026/06"
        
        # 3. Verify primary query path_prefix parameter was normalized and passed
        assert kwargs["path_prefix"] == "second-brain/Editors/Giulio"
