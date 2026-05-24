"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import argparse
import json
import logging
import os
import sys

# Parse command line arguments to override environment variables BEFORE importing config modules!
parser = argparse.ArgumentParser(description="Obsidian Vault MCP Server", add_help=False)
parser.add_argument("--port", type=int, help="Port to run the MCP server on")
parser.add_argument("--root-path", type=str, help="Root path prefix for routing (e.g. /giulio)")
parser.add_argument("--vault-path", type=str, help="Path to the Obsidian vault")
parser.add_argument("--db-path", type=str, help="Path to the SQLite search index database")
parser.add_argument("--access-db-path", type=str, help="Path to the SQLite access tracker database")
parser.add_argument("--mcp-name", type=str, help="Name of the MCP server instance")
parser.add_argument("-h", "--help", action="store_true", help="Show this help message and exit")

args, _ = parser.parse_known_args()

if args.help:
    parser.print_help()
    sys.exit(0)

if args.port:
    os.environ["VAULT_MCP_PORT"] = str(args.port)
if args.root_path:
    os.environ["VAULT_MCP_ROOT_PATH"] = args.root_path
if args.vault_path:
    os.environ["VAULT_PATH"] = args.vault_path
if args.db_path:
    os.environ["VAULT_DB_PATH"] = args.db_path
if args.access_db_path:
    os.environ["ACCESS_DB_PATH"] = args.access_db_path
if args.mcp_name:
    os.environ["VAULT_MCP_NAME"] = args.mcp_name

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import VAULT_MCP_PORT, VAULT_MCP_TOKEN, VAULT_MCP_HOST, VAULT_MCP_HOSTNAME, VAULT_PATH
from .frontmatter_index import FrontmatterIndex
from .access_tracker import AccessTracker

logger = logging.getLogger(__name__)

# Global frontmatter index instance
frontmatter_index = FrontmatterIndex()

# Global access tracker for Ebbinghaus retention scoring
access_tracker = AccessTracker()


@asynccontextmanager
async def lifespan(server):
    """Start frontmatter index on server startup, stop on shutdown."""
    logger.info(f"Starting vault MCP server. Vault: {VAULT_PATH}")
    frontmatter_index.start()
    logger.info(f"Frontmatter index built: {frontmatter_index.file_count} files indexed")
    yield {"frontmatter_index": frontmatter_index}
    frontmatter_index.stop()
    logger.info("Vault MCP server shut down.")


# Build allowed_hosts: always include localhost variants; add public hostname if configured
_allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
if VAULT_MCP_HOSTNAME:
    _allowed_hosts.append(VAULT_MCP_HOSTNAME)
    _allowed_hosts.append(f"{VAULT_MCP_HOSTNAME}:443")

# Create the MCP server
mcp = FastMCP(
    os.environ.get("VAULT_MCP_NAME", "obsidian_web_mcp"),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=_allowed_hosts,
    ),
)


# --- Register all tools ---

from .tools.read import vault_read as _vault_read, vault_batch_read as _vault_batch_read
from .tools.write import vault_write as _vault_write, vault_batch_frontmatter_update as _vault_batch_frontmatter_update
from .tools.write_advanced import vault_patch as _vault_patch, vault_append as _vault_append, vault_batch_write as _vault_batch_write, vault_extract_to_new_note as _vault_extract_to_new_note
from .tools.search import vault_search as _vault_search, vault_search_frontmatter as _vault_search_frontmatter
from .tools.manage import vault_list as _vault_list, vault_move as _vault_move, vault_delete as _vault_delete, vault_get_backlinks as _vault_get_backlinks
from .models import (
    VaultReadInput,
    VaultWriteInput,
    VaultBatchReadInput,
    VaultBatchFrontmatterUpdateInput,
    VaultSearchInput,
    VaultSearchFrontmatterInput,
    VaultListInput,
    VaultMoveInput,
    VaultDeleteInput,
)


@mcp.tool(
    name="vault_read",
    description="Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_read(path: str) -> str:
    """Read a file from the vault."""
    inp = VaultReadInput(path=path)
    result = _vault_read(inp.path)
    try:
        access_tracker.record(inp.path, source="read")
    except Exception:
        pass  # tracking must never break reads
    return result


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    result = _vault_batch_read(inp.paths, inp.include_content)
    try:
        access_tracker.record_batch(inp.paths, source="batch_read")
    except Exception:
        pass  # tracking must never break reads
    return result


@mcp.tool(
    name="vault_write",
    description=(
        "Write a file to the Obsidian vault. Supports frontmatter merging with existing files. Creates parent directories by default. "
        "ATTENTION: Before writing or modifying files, you MUST read the policies in System/agent.md. "
        "CRITICAL: Filenames and paths must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False) -> str:
    """Write a file to the vault."""
    path = path.replace("—", "-").replace("–", "-")
    inp = VaultWriteInput(path=path, content=content, create_dirs=create_dirs, merge_frontmatter=merge_frontmatter)
    return _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter)


@mcp.tool(
    name="vault_batch_frontmatter_update",
    description="Update YAML frontmatter fields on multiple files without changing body content. Each update merges new fields into existing frontmatter.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_frontmatter_update(updates: list[dict]) -> str:
    """Batch update frontmatter fields."""
    inp = VaultBatchFrontmatterUpdateInput(updates=updates)
    return _vault_batch_frontmatter_update(inp.updates)


@mcp.tool(
    name="vault_search",
    description="Search for text across vault files. Uses ripgrep if available, falls back to Python. Returns matching lines with context and frontmatter excerpts. CRITICAL: Technical notes are in English, personal notes in Italian. Agents MUST query in English for technical/architectural topics.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search vault file contents."""
    inp = VaultSearchInput(query=query, path_prefix=path_prefix, file_pattern=file_pattern, max_results=max_results, context_lines=context_lines)
    return _vault_search(inp.query, inp.path_prefix, inp.file_pattern, inp.max_results, inp.context_lines)


@mcp.tool(
    name="vault_search_frontmatter",
    description="Search vault files by YAML frontmatter field values. Queries an in-memory index for fast results. Supports exact match, contains, and field-exists queries.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search by frontmatter fields."""
    inp = VaultSearchFrontmatterInput(field=field, value=value, match_type=match_type, path_prefix=path_prefix, max_results=max_results)
    return _vault_search_frontmatter(inp.field, inp.value, inp.match_type, inp.path_prefix, inp.max_results)


@mcp.tool(
    name="vault_list",
    description="List directory contents in the vault. Supports recursion depth, file/dir filtering, and glob patterns. Excludes .obsidian, .trash, .git directories.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List vault directory contents."""
    inp = VaultListInput(path=path, depth=depth, include_files=include_files, include_dirs=include_dirs, pattern=pattern)
    return _vault_list(inp.path, inp.depth, inp.include_files, inp.include_dirs, inp.pattern)


@mcp.tool(
    name="vault_move",
    description=(
        "Move a file or directory within the vault. "
        "With update_links=True (default), automatically rewrites all [[wikilinks]] "
        "and markdown links pointing to the moved file using the in-memory link graph — "
        "no vault scan needed. Mirrors Obsidian Desktop's auto-link-update behaviour. "
        "CRITICAL: Destination filenames and paths must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_move(source: str, destination: str, create_dirs: bool = True, update_links: bool = True) -> str:
    """Move a file or directory, optionally rewriting backlinks."""
    source = source.replace("—", "-").replace("–", "-")
    destination = destination.replace("—", "-").replace("–", "-")
    inp = VaultMoveInput(source=source, destination=destination, create_dirs=create_dirs)
    return _vault_move(inp.source, inp.destination, inp.create_dirs, update_links)


@mcp.tool(
    name="vault_patch",
    description=(
        "Surgically replace a unique string in a vault file (works on frontmatter and body). "
        "old_str must appear EXACTLY ONCE — if ambiguous, include more surrounding lines. "
        "Returns a diff summary. Preferred over vault_write for targeted edits."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def vault_patch(path: str, old_str: str, new_str: str) -> str:
    """Surgical str_replace on a vault file."""
    return _vault_patch(path, old_str, new_str)


@mcp.tool(
    name="vault_append",
    description=(
        "Append content to the end of a vault file, or insert it after a specific ## section heading. "
        "Use for adding log entries, todo items, or new sections without rewriting the whole file. "
        "Creates the file if it does not exist. "
        "ATTENTION: Before writing or modifying files, you MUST read the policies in System/agent.md. "
        "CRITICAL: Filenames and paths must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def vault_append(
    path: str,
    content: str,
    after_section: str | None = None,
    create_if_missing: bool = True,
) -> str:
    """Append content to a vault file."""
    path = path.replace("—", "-").replace("–", "-")
    return _vault_append(path, content, after_section, create_if_missing)


@mcp.tool(
    name="vault_batch_write",
    description=(
        "Create or update multiple vault files in a single call. "
        "Each item needs 'path' and 'content'; optionally 'merge_frontmatter': true. "
        "More efficient than calling vault_write repeatedly for bulk note creation. "
        "ATTENTION: Before writing or modifying files, you MUST read the policies in System/agent.md. "
        "CRITICAL: Filenames and paths must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_batch_write(files: list[dict]) -> str:
    """Create or update multiple files at once."""
    for f in files:
        if "path" in f:
            f["path"] = f["path"].replace("—", "-").replace("–", "-")
    return _vault_batch_write(files)


@mcp.tool(
    name="vault_extract_to_new_note",
    description=(
        "Surgically extract text from one note, replace it with a [[wikilink]], and create a new atomic note. "
        "Use this for Zettelkasten refactoring when a note becomes too monolithic. "
        "ATTENTION: Before writing or modifying files, you MUST read the policies in System/agent.md. "
        "CRITICAL: The new note filename and path must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_extract_to_new_note(source_path: str, selection_to_extract: str, new_note_path: str, new_note_content: str | None = None) -> str:
    """Extract text to a new atomic note and link it."""
    source_path = source_path.replace("—", "-").replace("–", "-")
    new_note_path = new_note_path.replace("—", "-").replace("–", "-")
    return _vault_extract_to_new_note(source_path, selection_to_extract, new_note_path, new_note_content)


@mcp.tool(
    name="vault_get_backlinks",
    description=(
        "Return all notes that link TO a given file (backlinks) and all notes "
        "the file links to (forward links). Uses the in-memory link graph — O(1), no scan. "
        "Use before vault_move to preview which notes will be updated."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_get_backlinks(path: str) -> str:
    """Inspect the link graph for a given note."""
    return _vault_get_backlinks(path)


@mcp.tool(
    name="vault_delete",
    description="Delete a file by moving it to .trash/ in the vault root. Requires confirm=true as a safety gate. Does NOT hard delete.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file (move to .trash/)."""
    inp = VaultDeleteInput(path=path, confirm=confirm)
    return _vault_delete(inp.path, inp.confirm)


@mcp.tool(
    name="query_vault",
    description=(
        "Hybrid semantic + keyword search across the Obsidian Knowledge Base. "
        "Combines BM25 full-text search with vector similarity (Vertex AI embeddings) "
        "and Reciprocal Rank Fusion for high-quality retrieval. "
        "Use this as the PRIMARY method to find relevant notes — prefer it over vault_search "
        "for any conceptual or open-ended question. "
        "Set rerank=True when the query is complex, ambiguous, or multi-concept (adds ~2s latency). "
        "Set rerank=False (default) for simple keyword lookups.\n"
        "Set expand=True to enrich the query using a Vault-Aware Knowledge Map (SLM).\n"
        "CRITICAL: Technical notes are in English, personal notes in Italian. "
        "Agents MUST query in English for technical/architectural topics."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def query_vault(
    query: str,
    top_k: int = 5,
    rerank: bool = False,
    expand: bool = False,
    path_filter: str | None = None,
) -> str:
    """Hybrid semantic search over the vault Knowledge Base.

    Args:
        query:       Natural language query or keyword string.
        top_k:       Number of results to return (default 5, max 20).
        rerank:      If True, use Gemini Flash to re-score the top candidates.
                     Enable for complex/ambiguous queries. Adds ~2s latency.
        expand:      If True, generate query variants via Gemini using a vault
                     knowledge map to improve recall for paraphrased concepts.
        path_filter: Optional vault-relative path prefix to restrict search
                     (e.g. 'projects/' to search only the projects folder).
    """
    import json
    from .qmd.db import QMDDatabase
    from .qmd.search_engine import HybridSearchEngine
    from .qmd.vertex_client import embed_query, expand_query, rerank_chunks
    from .retention_engine import compute_retention, DEFAULT_CONFIG as DECAY_CONFIG

    top_k = max(1, min(top_k, 20))  # clamp

    try:
        with QMDDatabase() as db:
            stats = db.stats()
            if stats["chunks"] == 0:
                return json.dumps({
                    "error": "QMD index is empty. Run: uv run qmd-index --full --vault <path>",
                    "results": [],
                })

            engine = HybridSearchEngine(db)

            # Query expansion: generate alternative phrasings using Vault Knowledge Map
            if expand:
                knowledge_map = frontmatter_index.get_knowledge_map_summary()
                queries = expand_query(query, knowledge_map=knowledge_map)
            else:
                queries = None

            # Reranker: only wire it up if the agent requested it
            rerank_fn = rerank_chunks if rerank else None

            def get_links(p: str) -> tuple[set[str], set[str]]:
                fwd = frontmatter_index.get_forward_links(p)
                back = frontmatter_index.get_backlinks(p)
                return fwd, back

            results = engine.search(
                query=query,
                top_k=top_k,
                queries=queries[1:] if queries else None,  # extras only, primary is first
                embed_fn=embed_query,
                rerank_fn=rerank_fn,
                get_links_fn=get_links,
            )

            # ── Retention scoring (Lazy Ebbinghaus) ──────────────────────────
            # Build a map of doc_path → retention_boost for the results we got.
            # We only score the docs we retrieved (not the entire vault) — lazy.
            retention_boosts: dict[str, float] = {}
            retention_scores_map: dict[str, float] = {}
            try:
                unique_paths = list({r.doc_path for r in results})
                # Fetch mtime from QMD documents table (already stored there)
                mtime_map: dict[str, float] = {}
                for row in db.conn.execute(
                    f"SELECT path, mtime FROM documents WHERE path IN ({','.join('?' * len(unique_paths))})",
                    unique_paths,
                ):
                    mtime_map[row["path"]] = row["mtime"]

                for path in unique_paths:
                    stats_entry = access_tracker.get_stats(path)
                    fm = frontmatter_index.get_frontmatter(path)
                    note_type = fm.get("type") if fm else None
                    created_at_ts = mtime_map.get(path)
                    rs = compute_retention(
                        path=path,
                        note_type=note_type,
                        created_at_ts=created_at_ts,
                        access_stats=stats_entry,
                        config=DECAY_CONFIG,
                    )
                    retention_boosts[path] = rs.as_rrf_boost
                    retention_scores_map[path] = round(rs.score, 4)
            except Exception as e:
                logger.debug(f"Retention scoring skipped: {e}")
            # ─────────────────────────────────────────────────────────────────


            # Apply path filter post-retrieval (simple prefix match)
            if path_filter:
                results = [r for r in results if r.doc_path.startswith(path_filter)]

            output = [
                {
                    "rank": i + 1,
                    "score": round(r.score + retention_boosts.get(r.doc_path, 0.0), 4),
                    "retention": retention_scores_map.get(r.doc_path, None),
                    "path": r.doc_path,
                    "title": r.doc_title,
                    "section": r.header_path,
                    "obsidian_link": r.obsidian_link,
                    "snippet": r.snippet,
                    "sources": r.sources,
                }
                for i, r in enumerate(results)
            ]

            # Track accessed paths for retention scoring
            try:
                accessed_paths = list({r.doc_path for r in results})
                if accessed_paths:
                    access_tracker.record_batch(accessed_paths, source="query")
            except Exception:
                pass  # tracking must never break queries

            return json.dumps({
                "query": query,
                "expanded_queries": queries[1:] if queries else [],
                "reranked": rerank,
                "total": len(output),
                "index_stats": {"chunks": stats["chunks"], "documents": stats["documents"]},
                "results": output,
            }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"query_vault error: {e}")
        return json.dumps({"error": str(e), "results": []})


@mcp.tool(
    name="vault_save_reference",
    description=(
        "Downloads a web page, extracts its clean main content (defuddle), and saves it as a Markdown file in /Library/Web/, updating the index. "
        "ATTENTION: Before operating, you MUST read the policies in System/agent.md. "
        "CRITICAL: Filenames and paths must NOT contain em-dashes (—). Replace them with regular hyphens (-) or spaces."
    )
)
def vault_save_reference(url: str, title_override: str | None = None) -> str:
    import json
    import time
    from slugify import slugify
    import trafilatura
    import frontmatter
    from .qmd.db import QMDDatabase
    from .qmd.indexer import VaultIndexer

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return json.dumps({"error": f"Failed to download URL: {url}"})

        # Extract markdown, include links, no images to keep it clean
        extracted = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=True,
            include_images=False,
            include_formatting=True
        )
        if not extracted:
            return json.dumps({"error": "Failed to extract content (maybe not an article?)"})

        metadata = trafilatura.extract_metadata(downloaded)
        title = title_override or (metadata.title if metadata and metadata.title else "Untitled Reference")
        
        # Build frontmatter
        post = frontmatter.Post(extracted)
        post.metadata["type"] = "reference"
        post.metadata["url"] = url
        post.metadata["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        post.metadata["title"] = title
        if metadata and metadata.author:
            post.metadata["author"] = metadata.author
        if metadata and metadata.date:
            post.metadata["published_date"] = metadata.date

        title_clean = title.replace("—", "-").replace("–", "-")
        # Slugify title for filename
        slug = slugify(title_clean, max_length=60)
        if not slug:
            slug = str(int(time.time()))
            
        file_name = f"{slug}.md"
        references_dir = VAULT_PATH / "Library" / "Web"
        references_dir.mkdir(parents=True, exist_ok=True)
        
        file_path = references_dir / file_name
        
        # Avoid overwriting existing references with the same slug by appending timestamp
        if file_path.exists():
            file_name = f"{slug}-{int(time.time())}.md"
            file_path = references_dir / file_name

        file_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        
        # Re-index only the new file via delta
        try:
            with QMDDatabase() as db:
                indexer = VaultIndexer(VAULT_PATH, db, embed=True)
                indexer.run_delta()
        except Exception as e:
            logger.warning(f"Indexing failed for new reference: {e}")
            return json.dumps({
                "success": True,
                "path": f"References/{file_name}",
                "warning": f"Saved, but indexing failed: {e}"
            })

        return json.dumps({
            "success": True,
            "path": f"References/{file_name}",
            "title": title
        })

    except Exception as e:
        logger.error(f"vault_save_reference error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="vault_sync_context",
    description="Synchronizes the local knowledge base state with external sources to guarantee data consistency. Use this when User directly or indirectly "\
        "mentions he has **recently** made local changes to the knowledge base."
)
def vault_sync_context() -> str:
    """Force updating the vault cache by synchronizing with the cloud."""
    from .rclone_sync import sync_vault
    if sync_vault():
        return json.dumps({
            "success": True,
            "message": "Synchronization completed successfully. The knowledge base is now up to date."
        })
    else:
        return json.dumps({
            "success": False,
            "error": "Error during knowledge base synchronization. Check the server logs."
        })


def main():
    """Entry point. Run with streamable HTTP transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not VAULT_PATH.is_dir():
        logger.error(f"Vault path does not exist: {VAULT_PATH}")
        sys.exit(1)

    if not VAULT_MCP_TOKEN:
        logger.warning("VAULT_MCP_TOKEN is not set -- auth will reject all requests")

    # Build the Starlette app with auth middleware and OAuth endpoints
    try:
        from .auth import BearerAuthMiddleware
        from .oauth import oauth_routes

        app = mcp.streamable_http_app()

        # Mount OAuth routes (these are excluded from bearer auth via the middleware)
        for route in oauth_routes:
            app.routes.insert(0, route)

        app.add_middleware(BearerAuthMiddleware)
        
        # Configure root_path routing prefix if specified
        root_path = os.environ.get("VAULT_MCP_ROOT_PATH", "")
        if root_path:
            logger.info(f"Root path configured for Uvicorn: {root_path}")

        logger.info(f"Starting server on port {VAULT_MCP_PORT} with bearer auth + OAuth")
        
        logger.info(f"Avvio indicizzazione globale del Vault: {VAULT_PATH}")
        frontmatter_index.start()

        # Start access tracker for retention scoring
        access_tracker.open()
        logger.info(f"Access tracker avviato: {access_tracker.stats()}")

        import uvicorn
        uvicorn_args = {
            "app": app,
            "host": VAULT_MCP_HOST,
            "port": VAULT_MCP_PORT,
            "log_level": "info",
            "proxy_headers": True,
            "forwarded_allow_ips": "*",
        }
        if root_path:
            uvicorn_args["root_path"] = root_path

        uvicorn.run(**uvicorn_args)
    except Exception as e:
        logger.warning(f"Could not build app ({e}), falling back to mcp.run()")
        logger.warning("Auth will NOT be enforced in this mode")
        mcp.run(transport="streamable-http", port=VAULT_MCP_PORT)


if __name__ == "__main__":
    main()
