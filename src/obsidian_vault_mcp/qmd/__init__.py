"""QMD-Lite: Hybrid semantic search for Obsidian vault.

Package structure:
  chunker.py       - Markdown chunking logic
  vertex_client.py - Vertex AI embedding + Gemini client
  db.py            - SQLite + FTS5 + sqlite-vec storage
  indexer.py       - Full + incremental vault indexer
"""
