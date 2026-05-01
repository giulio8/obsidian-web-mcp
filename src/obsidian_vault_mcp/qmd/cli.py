"""CLI scripts for QMD-Lite: indexer (qmd-index) and search tester (qmd-search).

Usage (on the VM):
    uv run qmd-index              # incremental (default)
    uv run qmd-index --full       # full re-index
    uv run qmd-index --stats      # show DB stats only
    uv run qmd-index --no-embed   # index text only, skip Vertex AI embeddings

    uv run qmd-search "come funzionano i Transformer?"   # hybrid (BM25 + vector)
    uv run qmd-search "IPv6" --bm25                      # BM25 only, no API
    uv run qmd-search "agenti AI" --top-k 10             # more results
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Load .env if running locally (on VM, systemd EnvironmentFile handles this)
def _try_load_dotenv():
    try:
        repo_root = Path(__file__).parent.parent.parent.parent.parent
        env_file = repo_root / ".env"
        if not env_file.exists():
            env_file = Path.home() / "obsidian-web-mcp" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except Exception:
        pass


def main():
    _try_load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("qmd-index")

    parser = argparse.ArgumentParser(description="QMD-Lite vault indexer")
    parser.add_argument("--full", action="store_true", help="Force full re-index")
    parser.add_argument("--stats", action="store_true", help="Show DB stats and exit")
    parser.add_argument(
        "--no-embed", action="store_true", help="Skip Vertex AI embedding (BM25 only)"
    )
    parser.add_argument(
        "--vault", type=Path, help="Override VAULT_PATH env var"
    )
    args = parser.parse_args()

    # Resolve vault path
    vault_path = args.vault or Path(os.environ.get("VAULT_PATH", ""))
    if not vault_path or not vault_path.is_dir():
        logger.error(
            f"Vault path does not exist or is not set: {vault_path!r}\n"
            "Set VAULT_PATH in .env or use --vault /path/to/vault"
        )
        sys.exit(1)

    # Import here to avoid loading DB/modules at import time
    from obsidian_vault_mcp.qmd.db import QMDDatabase
    from obsidian_vault_mcp.qmd.indexer import VaultIndexer

    with QMDDatabase() as db:
        if args.stats:
            stats = db.stats()
            print("\n── QMD-Lite Index Status ──")
            for k, v in stats.items():
                print(f"  {k:20s}: {v}")
            print()
            return

        indexer = VaultIndexer(vault_path, db, embed=not args.no_embed)
        run_stats = indexer.run_full() if args.full else indexer.run_delta()

        print("\n── Indexing Complete ──")
        print(f"  indexed    : {run_stats.indexed}")
        print(f"  updated    : {run_stats.updated}")
        print(f"  unchanged  : {run_stats.unchanged}")
        print(f"  removed    : {run_stats.removed}")
        print(f"  chunks     : {run_stats.chunks_added}")
        print(f"  errors     : {run_stats.errors}")
        print(f"  elapsed    : {run_stats.elapsed_sec:.1f}s")
        print()


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────────
# qmd-search  —  manual search testing
# ─────────────────────────────────────────────────────────────────────────────

def search_main():
    """Entry point for `uv run qmd-search`."""
    _try_load_dotenv()

    logging.basicConfig(
        level=logging.WARNING,  # suppress debug noise during search
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="QMD-Lite hybrid search")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results")
    parser.add_argument(
        "--bm25", action="store_true", help="BM25-only mode (no Vertex AI)"
    )
    parser.add_argument(
        "--full-text", action="store_true", help="Print full chunk text instead of snippet"
    )
    args = parser.parse_args()

    from obsidian_vault_mcp.qmd.db import QMDDatabase
    from obsidian_vault_mcp.qmd.search_engine import HybridSearchEngine

    with QMDDatabase() as db:
        stats = db.stats()
        if stats["chunks"] == 0:
            print("[!] Index is empty. Run: uv run qmd-index --full --vault <path>")
            sys.exit(1)

        engine = HybridSearchEngine(db)

        if args.bm25:
            results = engine.bm25_only(args.query, top_k=args.top_k)
        else:
            from obsidian_vault_mcp.qmd.vertex_client import embed_query
            results = engine.search(
                args.query,
                top_k=args.top_k,
                embed_fn=embed_query,
            )

        if not results:
            print(f"Nessun risultato per: {args.query!r}")
            sys.exit(0)

        print(f"\n── Risultati per: {args.query!r} ──\n")
        for i, r in enumerate(results):
            header = f" › {r.header_path}" if r.header_path else ""
            sources = "+".join(r.sources) if r.sources else "?"
            print(f"[{i+1}] {r.obsidian_link}{header}")
            print(f"    score={r.score:.3f} ({sources})  —  {r.doc_title}")
            body = r.text if args.full_text else r.snippet
            for line in body.splitlines()[:8]:
                print(f"    {line}")
            print()


if __name__ == "__main__":
    search_main()
