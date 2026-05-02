"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import json
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import VAULT_MCP_PORT, VAULT_MCP_TOKEN, VAULT_PATH
from .frontmatter_index import FrontmatterIndex

logger = logging.getLogger(__name__)

# Global frontmatter index instance
frontmatter_index = FrontmatterIndex()


@asynccontextmanager
async def lifespan(server):
    """Start frontmatter index on server startup, stop on shutdown."""
    logger.info(f"Starting vault MCP server. Vault: {VAULT_PATH}")
    frontmatter_index.start()
    logger.info(f"Frontmatter index built: {frontmatter_index.file_count} files indexed")
    yield {"frontmatter_index": frontmatter_index}
    frontmatter_index.stop()
    logger.info("Vault MCP server shut down.")


# Create the MCP server
mcp = FastMCP(
    "obsidian_web_mcp",
    stateless_http=True,
    json_response=True,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            # Add your tunnel hostname here, e.g.:
            # "vault-mcp.example.com",
        ],
    ),
)


# --- Register all tools ---

from .tools.read import vault_read as _vault_read, vault_batch_read as _vault_batch_read
from .tools.write import vault_write as _vault_write, vault_batch_frontmatter_update as _vault_batch_frontmatter_update
from .tools.write_advanced import vault_patch as _vault_patch, vault_append as _vault_append, vault_batch_write as _vault_batch_write
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
    return _vault_read(inp.path)


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    return _vault_batch_read(inp.paths, inp.include_content)


@mcp.tool(
    name="vault_write",
    description="Write a file to the Obsidian vault. Supports frontmatter merging with existing files. Creates parent directories by default.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False) -> str:
    """Write a file to the vault."""
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
    description="Search for text across vault files. Uses ripgrep if available, falls back to Python. Returns matching lines with context and frontmatter excerpts.",
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
        "no vault scan needed. Mirrors Obsidian Desktop's auto-link-update behaviour."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_move(source: str, destination: str, create_dirs: bool = True, update_links: bool = True) -> str:
    """Move a file or directory, optionally rewriting backlinks."""
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
        "Creates the file if it does not exist."
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
    return _vault_append(path, content, after_section, create_if_missing)


@mcp.tool(
    name="vault_batch_write",
    description=(
        "Create or update multiple vault files in a single call. "
        "Each item needs 'path' and 'content'; optionally 'merge_frontmatter': true. "
        "More efficient than calling vault_write repeatedly for bulk note creation."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_batch_write(files: list[dict]) -> str:
    """Create or update multiple files at once."""
    return _vault_batch_write(files)


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
        "Set rerank=False (default) for simple keyword lookups."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def query_vault(
    query: str,
    top_k: int = 5,
    rerank: bool = False,
    expand: bool = True,
    path_filter: str | None = None,
) -> str:
    """Hybrid semantic search over the vault Knowledge Base.

    Args:
        query:       Natural language query or keyword string.
        top_k:       Number of results to return (default 5, max 20).
        rerank:      If True, use Gemini Flash to re-score the top candidates.
                     Enable for complex/ambiguous queries. Adds ~2s latency.
        expand:      If True (default), generate 1-2 query variants via Gemini
                     to improve recall for paraphrased concepts.
        path_filter: Optional vault-relative path prefix to restrict search
                     (e.g. 'projects/' to search only the projects folder).
    """
    import json
    from .qmd.db import QMDDatabase
    from .qmd.search_engine import HybridSearchEngine
    from .qmd.vertex_client import embed_query, expand_query, rerank_chunks

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

            # Query expansion: generate alternative phrasings for better recall
            queries = expand_query(query) if expand else None

            # Reranker: only wire it up if the agent requested it
            rerank_fn = rerank_chunks if rerank else None

            results = engine.search(
                query=query,
                top_k=top_k,
                queries=queries[1:] if queries else None,  # extras only, primary is first
                embed_fn=embed_query,
                rerank_fn=rerank_fn,
            )

            # Apply path filter post-retrieval (simple prefix match)
            if path_filter:
                results = [r for r in results if r.doc_path.startswith(path_filter)]

            output = [
                {
                    "rank": i + 1,
                    "score": round(r.score, 4),
                    "path": r.doc_path,
                    "title": r.doc_title,
                    "section": r.header_path,
                    "obsidian_link": r.obsidian_link,
                    "snippet": r.snippet,
                    "sources": r.sources,
                }
                for i, r in enumerate(results)
            ]

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
        logger.info(f"Starting server on port {VAULT_MCP_PORT} with bearer auth + OAuth")

        import uvicorn
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=VAULT_MCP_PORT,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    except Exception as e:
        logger.warning(f"Could not build app ({e}), falling back to mcp.run()")
        logger.warning("Auth will NOT be enforced in this mode")
        mcp.run(transport="streamable-http", port=VAULT_MCP_PORT)


if __name__ == "__main__":
    main()
