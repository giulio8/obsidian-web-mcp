"""CLI script for QMD-Lite indexer.

Usage (on the VM):
    uv run qmd-index              # incremental (default)
    uv run qmd-index --full       # full re-index
    uv run qmd-index --stats      # show DB stats only
    uv run qmd-index --no-embed   # index text only, skip Vertex AI embeddings

Environment variables (loaded from .env automatically by systemd EnvironmentFile):
    VAULT_PATH          - path to the mounted vault (required)
    GCP_PROJECT_ID      - GCP project for Vertex AI (required for embedding)
    GCP_REGION          - Vertex AI region (default: us-east1)
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
