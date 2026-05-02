"""Management tools for the Obsidian vault MCP server."""

import json
import logging
import re

from ..vault import list_directory, move_path, delete_path, resolve_vault_path
from ..rclone_sync import push_file, push_deleted

logger = logging.getLogger(__name__)

# Regex patterns for wikilink rewriting
# Matches [[old-name]], [[old-name|alias]], [[path/to/old-name]], [[path/to/old-name|alias]]
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+?)(\|[^\]]+)?\]\]")
# Matches [text](path/to/note.md)
_MDLINK_PATTERN = re.compile(r"(\[[^\]]*\]\()([^)]+\.md)(\))")


def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List directory contents in the vault."""
    try:
        items = list_directory(
            path,
            depth=depth,
            include_files=include_files,
            include_dirs=include_dirs,
            pattern=pattern,
        )
        return json.dumps({"items": items, "total": len(items)})
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"Directory not found: {path}"})
    except Exception as e:
        logger.error(f"vault_list error: {e}")
        return json.dumps({"error": str(e)})


def vault_move(
    source: str,
    destination: str,
    create_dirs: bool = True,
    update_links: bool = True,
) -> str:
    """Move a file or directory within the vault.

    If update_links=True (default), uses the in-memory LinkIndex to find all
    notes that link to the source file and rewrites those links atomically.
    No full vault scan is needed — the lookup is O(1).
    """
    from ..server import frontmatter_index
    from ..vault import write_file_atomic, read_file
    from pathlib import Path

    link_updates: list[dict] = []

    try:
        # --- Pre-move: collect backlinks from LinkIndex ---
        backlinks: set[str] = set()
        if update_links:
            backlinks = frontmatter_index.get_backlinks(source)
            logger.debug(f"vault_move: {len(backlinks)} backlink(s) to rewrite for {source!r}")

        # --- Filesystem move ---
        moved = move_path(source, destination, create_dirs=create_dirs)
        if moved:
            push_file(destination)
            push_deleted(source)

        # --- Post-move: rewrite backlinks ---
        if update_links and backlinks:
            old_stem = Path(source).stem
            new_stem = Path(destination).stem
            old_path_no_ext = source.removesuffix(".md")
            new_path_no_ext = destination.removesuffix(".md")

            for bl_path in backlinks:
                try:
                    content, _ = read_file(bl_path)
                    new_content = _rewrite_links(
                        content,
                        old_stem=old_stem,
                        new_stem=new_stem,
                        old_path=old_path_no_ext,
                        new_path=new_path_no_ext,
                    )
                    if new_content != content:
                        write_file_atomic(bl_path, new_content, create_dirs=False)
                        push_file(bl_path)
                        link_updates.append({"path": bl_path, "rewritten": True})
                except Exception as e:
                    logger.warning(f"Failed to rewrite links in {bl_path}: {e}")
                    link_updates.append({"path": bl_path, "rewritten": False, "error": str(e)})

        # --- Update the in-memory graph ---
        if moved:
            frontmatter_index.rename_in_graph(source, destination)

        return json.dumps({
            "source": source,
            "destination": destination,
            "moved": moved,
            "links_updated": len([u for u in link_updates if u.get("rewritten")]),
            "link_updates": link_updates,
        })

    except ValueError as e:
        return json.dumps({"error": str(e), "source": source, "destination": destination})
    except Exception as e:
        logger.error(f"vault_move error: {e}")
        return json.dumps({"error": str(e), "source": source, "destination": destination})


def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file by moving it to .trash/ in the vault."""
    if not confirm:
        return json.dumps({
            "error": "Set confirm=true to execute deletion. Files are moved to .trash/, not hard deleted.",
            "path": path,
        })

    try:
        deleted = delete_path(path)
        if deleted:
            push_deleted(path)
        return json.dumps({"path": path, "deleted": deleted})
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_delete error: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_get_backlinks(path: str) -> str:
    """Return all notes that link to the given file (backlinks) and all notes
    that the given file links to (forward links).

    Uses the in-memory LinkIndex — O(1), no filesystem scan.
    Useful for understanding the impact of a rename before executing it,
    or for exploring the knowledge graph around a note.

    Args:
        path: Vault-relative path to the note (e.g. "projects/qmd.md").
    """
    from ..server import frontmatter_index

    try:
        backlinks = sorted(frontmatter_index.get_backlinks(path))
        forward_links = sorted(frontmatter_index.get_forward_links(path))
        return json.dumps({
            "path": path,
            "backlinks": backlinks,
            "backlink_count": len(backlinks),
            "forward_links": forward_links,
            "forward_link_count": len(forward_links),
        })
    except Exception as e:
        logger.error(f"vault_get_backlinks error: {e}")
        return json.dumps({"error": str(e), "path": path})


# ─────────────────────────────────────────────────────────────────────────────
# Internal: link rewriting
# ─────────────────────────────────────────────────────────────────────────────

def _rewrite_links(
    content: str,
    old_stem: str,
    new_stem: str,
    old_path: str,
    new_path: str,
) -> str:
    """Rewrite wikilinks and markdown links in content after a file move.

    Handles:
      [[old-stem]]            → [[new-stem]]          (if stem changed)
      [[old-stem|alias]]      → [[new-stem|alias]]
      [[path/to/old]]         → [[path/to/new]]        (full path form)
      [[path/to/old|alias]]   → [[path/to/new|alias]]
      [text](path/to/old.md)  → [text](path/to/new.md)
    """
    def replace_wikilink(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = m.group(2) or ""  # includes the leading | if present

        # Match by stem or full path
        target_stem = target.split("/")[-1]
        if target == old_path or target_stem == old_stem:
            new_target = new_path if "/" in target else new_stem
            return f"[[{new_target}{alias}]]"
        return m.group(0)  # unchanged

    def replace_mdlink(m: re.Match) -> str:
        prefix = m.group(1)
        link_target = m.group(2)
        suffix = m.group(3)
        target_no_ext = link_target.removesuffix(".md")
        if target_no_ext == old_path or Path(target_no_ext).stem == old_stem:
            return f"{prefix}{new_path}.md{suffix}"
        return m.group(0)

    content = _WIKILINK_PATTERN.sub(replace_wikilink, content)
    content = _MDLINK_PATTERN.sub(replace_mdlink, content)
    return content
