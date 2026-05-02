"""Advanced write tools for surgical, non-destructive vault editing.

Inspired by Claude Code's str_replace_editor and Cursor's diff-based edits.
All operations are atomic (write-to-temp then replace) and trigger rclone push.

Tools:
  vault_patch       — str_replace on the full file (frontmatter + body)
  vault_append      — append content to end of file or after a ## section
  vault_batch_write — create/update multiple files in one call
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import frontmatter as fm_lib

from ..vault import read_file, write_file_atomic, resolve_vault_path
from ..rclone_sync import push_file

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# vault_patch
# ─────────────────────────────────────────────────────────────────────────────

def vault_patch(path: str, old_str: str, new_str: str) -> str:
    """Replace a unique string in a vault file (frontmatter or body).

    Mirrors the str_replace_editor pattern used by Claude Code:
    - old_str must appear EXACTLY ONCE in the file.
    - If it appears 0 or 2+ times, the operation is rejected with a clear error
      so the caller can add more context and retry.
    - Returns a unified-diff-style summary of the change.

    Args:
        path:    Vault-relative path to the file.
        old_str: The exact string to find and replace. Must be unique in the file.
        new_str: The replacement string. May be empty to delete old_str.
    """
    try:
        content, _ = read_file(path)
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})

    count = content.count(old_str)
    if count == 0:
        return json.dumps({
            "error": (
                "old_str not found in file. "
                "Check for whitespace differences or add more surrounding context."
            ),
            "path": path,
        })
    if count > 1:
        return json.dumps({
            "error": (
                f"old_str appears {count} times in the file — ambiguous replacement. "
                "Add more surrounding lines to make it unique."
            ),
            "path": path,
            "occurrences": count,
        })

    new_content = content.replace(old_str, new_str, 1)

    try:
        is_new, size = write_file_atomic(path, new_content)
        push_file(path)
    except Exception as e:
        return json.dumps({"error": str(e), "path": path})

    # Build a compact diff summary for the agent
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()
    diff_lines = (
        [f"- {l}" for l in old_lines] +
        [f"+ {l}" for l in new_lines]
    )
    return json.dumps({
        "path": path,
        "patched": True,
        "diff": "\n".join(diff_lines[:40]),  # cap at 40 lines
        "size": size,
    })


# ─────────────────────────────────────────────────────────────────────────────
# vault_append
# ─────────────────────────────────────────────────────────────────────────────

def vault_append(
    path: str,
    content: str,
    after_section: str | None = None,
    create_if_missing: bool = True,
) -> str:
    """Append content to a vault file without rewriting the whole thing.

    Args:
        path:              Vault-relative file path.
        content:           Text to append (include leading newline if needed).
        after_section:     If set, insert content immediately after the first
                           heading line that matches (e.g. "## Todo").
                           The match is case-insensitive and trims whitespace.
                           If the section is not found, returns an error.
        create_if_missing: If True and the file does not exist, create it.
    """
    try:
        existing_content, _ = read_file(path)
    except FileNotFoundError:
        if not create_if_missing:
            return json.dumps({"error": f"File not found: {path}"})
        existing_content = ""

    if after_section is None:
        # Simple append to end of file
        separator = "\n" if existing_content and not existing_content.endswith("\n") else ""
        new_content = existing_content + separator + content
    else:
        # Insert after the matching section heading
        target = after_section.strip().lower()
        lines = existing_content.splitlines(keepends=True)
        insert_pos: int | None = None

        for i, line in enumerate(lines):
            stripped = line.strip().lower()
            if stripped == target:
                # Find the end of this section (next heading of same or higher level)
                heading_depth = len(line) - len(line.lstrip("#"))
                insert_pos = i + 1
                for j in range(i + 1, len(lines)):
                    next_depth = len(lines[j]) - len(lines[j].lstrip("#"))
                    if lines[j].startswith("#") and next_depth <= heading_depth:
                        insert_pos = j
                        break
                else:
                    insert_pos = len(lines)
                break

        if insert_pos is None:
            return json.dumps({
                "error": f"Section not found: {after_section!r}",
                "path": path,
            })

        # Ensure content ends with newline before insertion point
        insert_text = content if content.endswith("\n") else content + "\n"
        lines.insert(insert_pos, insert_text)
        new_content = "".join(lines)

    try:
        is_new, size = write_file_atomic(path, new_content, create_dirs=True)
        push_file(path)
    except Exception as e:
        return json.dumps({"error": str(e), "path": path})

    return json.dumps({
        "path": path,
        "created": is_new,
        "appended_bytes": len(content.encode()),
        "total_size": size,
    })


# ─────────────────────────────────────────────────────────────────────────────
# vault_batch_write
# ─────────────────────────────────────────────────────────────────────────────

def vault_batch_write(files: list[dict]) -> str:
    """Create or update multiple vault files in a single call.

    Each item in `files` is a dict with:
      - path (str, required)
      - content (str, required)
      - merge_frontmatter (bool, optional, default False)

    Processes all files even if some fail. Returns per-file results.

    Example:
        vault_batch_write(files=[
            {"path": "daily/2026-05-02.md", "content": "# Daily\\n..."},
            {"path": "inbox/idea.md", "content": "...", "merge_frontmatter": True},
        ])
    """
    import frontmatter as _fm

    results = []

    for item in files:
        file_path = item.get("path", "")
        content = item.get("content", "")
        merge = item.get("merge_frontmatter", False)

        if not file_path:
            results.append({"path": "", "error": "Missing 'path' field"})
            continue

        try:
            if merge:
                try:
                    existing_content, _ = read_file(file_path)
                    existing_post = _fm.loads(existing_content)
                    new_post = _fm.loads(content)
                    merged_meta = dict(existing_post.metadata)
                    merged_meta.update(new_post.metadata)
                    new_post.metadata = merged_meta
                    content = _fm.dumps(new_post)
                except FileNotFoundError:
                    pass  # New file — write as-is

            is_new, size = write_file_atomic(file_path, content, create_dirs=True)
            push_file(file_path)
            results.append({"path": file_path, "created": is_new, "size": size})

        except Exception as e:
            logger.error(f"vault_batch_write error for {file_path}: {e}")
            results.append({"path": file_path, "error": str(e)})

    total = len(results)
    failed = sum(1 for r in results if "error" in r)
    return json.dumps({
        "total": total,
        "succeeded": total - failed,
        "failed": failed,
        "results": results,
    })
