"""Tests for path normalization logic in server.py."""

import pytest
from unittest.mock import patch
from obsidian_vault_mcp.server import _normalize_path


def test_normalize_path_no_prefix():
    """When VAULT_RCLONE_PREFIX is not set, normalization shouldn't prepend anything."""
    with patch("obsidian_vault_mcp.config.VAULT_RCLONE_PREFIX", ""):
        # Standard paths
        assert _normalize_path("note.md") == "note.md"
        assert _normalize_path("folder/note.md") == "folder/note.md"
        
        # Leading slash
        assert _normalize_path("/note.md") == "note.md"
        assert _normalize_path("///folder/note.md") == "folder/note.md"
        
        # Empty paths
        assert _normalize_path("") == ""
        assert _normalize_path("/") == ""
        
        # Em-dashes and en-dashes
        assert _normalize_path("my—note–title.md") == "my-note-title.md"


def test_normalize_path_with_prefix():
    """When VAULT_RCLONE_PREFIX is set, normalization should prepend it when missing."""
    with patch("obsidian_vault_mcp.config.VAULT_RCLONE_PREFIX", "second-brain"):
        # Standard paths missing prefix
        assert _normalize_path("note.md") == "second-brain/note.md"
        assert _normalize_path("folder/note.md") == "second-brain/folder/note.md"
        
        # Paths with leading slashes missing prefix
        assert _normalize_path("/note.md") == "second-brain/note.md"
        assert _normalize_path("///folder/note.md") == "second-brain/folder/note.md"
        
        # Paths already containing the prefix (idempotency)
        assert _normalize_path("second-brain/note.md") == "second-brain/note.md"
        assert _normalize_path("second-brain/folder/note.md") == "second-brain/folder/note.md"
        assert _normalize_path("/second-brain/folder/note.md") == "second-brain/folder/note.md"
        
        # Empty and root paths should resolve to the prefix directory
        assert _normalize_path("") == "second-brain"
        assert _normalize_path("/") == "second-brain"
        assert _normalize_path("///") == "second-brain"
        
        # Em-dashes and en-dashes with prefix prepending
        assert _normalize_path("my—note–title.md") == "second-brain/my-note-title.md"
        assert _normalize_path("second-brain/my—note–title.md") == "second-brain/my-note-title.md"
