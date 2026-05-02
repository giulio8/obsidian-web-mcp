"""In-memory index of YAML frontmatter AND wikilinks across all vault .md files.

Two indexes maintained in parallel, updated by the same watchdog observer:

  FrontmatterIndex  — {rel_path → frontmatter dict}    (query by field/value)
  LinkIndex         — forward + backlink graph          (O(1) backlink lookup)

The LinkIndex enables:
  - vault_move with update_links=True (no full vault scan needed)
  - vault_get_backlinks (instant graph inspection)
  - Future graph-aware features (orphan detection, etc.)

Wikilink formats parsed:
  [[note-name]]
  [[note-name|alias]]
  [[path/to/note]]
  [[path/to/note|alias]]
  [text](path/to/note.md)      (markdown link to .md file)
"""

import logging
import re
import threading
import time
from pathlib import Path

import frontmatter
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config

logger = logging.getLogger(__name__)

# Regex for [[wikilinks]] with optional alias
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
# Regex for markdown links to .md files: [text](path.md)
_MDLINK_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)]+\.md)\)")


def _parse_links(content: str) -> set[str]:
    """Extract all linked note stems/paths from markdown content.

    Returns a set of raw link targets (not yet resolved to vault paths).
    E.g. {"note-name", "path/to/note", "other-note"}.
    """
    links: set[str] = set()
    for m in _WIKILINK_RE.finditer(content):
        links.add(m.group(1).strip())
    for m in _MDLINK_RE.finditer(content):
        target = m.group(1).strip()
        # Strip leading ./ if present
        if target.startswith("./"):
            target = target[2:]
        # Remove .md extension to normalise
        if target.endswith(".md"):
            target = target[:-3]
        links.add(target)
    return links


def _resolve_link(raw: str, source_path: str, all_paths: set[str]) -> str | None:
    """Resolve a raw link target to a vault-relative path.

    Strategy (mirrors Obsidian's resolution order):
      1. Exact match with .md extension against known paths
      2. Stem-only match (any path whose stem == raw)
      3. If nothing matches, return None (broken link)
    """
    # Try exact match (raw may include subdirectory)
    candidate = raw if raw.endswith(".md") else raw + ".md"
    if candidate in all_paths:
        return candidate

    # Try stem-only match
    raw_stem = Path(raw).name  # last component without extension
    for p in all_paths:
        if Path(p).stem == raw_stem:
            return p

    return None  # unresolvable broken link


class FrontmatterIndex:
    """Thread-safe in-memory index of YAML frontmatter for fast queries."""

    def __init__(self) -> None:
        self._index: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._debounce_timer: threading.Timer | None = None
        self._pending_paths: set[str] = set()

        # ── LinkIndex state ──────────────────────────────────────────────────
        # forward_links[path] = set of vault-relative paths this note links TO
        self._forward_links: dict[str, set[str]] = {}
        # backlinks[path] = set of vault-relative paths that link TO this note
        self._backlinks: dict[str, set[str]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Walk all .md files, parse frontmatter + links, start watching."""
        t0 = time.monotonic()
        count = 0

        all_paths: set[str] = set()
        file_contents: dict[str, str] = {}

        # First pass: collect all .md paths + frontmatter
        for md_path in config.VAULT_PATH.rglob("*.md"):
            if self._is_excluded(md_path):
                continue
            rel = str(md_path.relative_to(config.VAULT_PATH))
            all_paths.add(rel)

            fm = self._parse_frontmatter(md_path)
            if fm is not None:
                self._index[rel] = fm
                count += 1

            try:
                file_contents[rel] = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        # Second pass: build link graph (needs all_paths for resolution)
        for rel, content in file_contents.items():
            self._update_links_for(rel, content, all_paths)

        elapsed = time.monotonic() - t0
        logger.info(
            "Frontmatter index built: %d files in %.2f seconds "
            "(link graph: %d nodes, %d edges)",
            count,
            elapsed,
            len(self._forward_links),
            sum(len(v) for v in self._forward_links.values()),
        )

        self._observer = Observer()
        handler = _VaultEventHandler(self)
        self._observer.schedule(handler, str(config.VAULT_PATH), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        """Stop the filesystem observer and cancel any pending debounce."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    @property
    def file_count(self) -> int:
        with self._lock:
            return len(self._index)

    # ── FrontmatterIndex queries ───────────────────────────────────────────────

    def search_by_field(
        self,
        field: str,
        value: str,
        match_type: str,
        path_prefix: str | None = None,
    ) -> list[dict]:
        """Search frontmatter index by field.

        Args:
            field: Frontmatter key to match against.
            value: Value to compare (ignored for match_type "exists").
            match_type: One of "exact", "contains", "exists".
            path_prefix: If set, only return files whose relative path starts with this.

        Returns:
            List of {"path": relative_path, "frontmatter": dict}.
        """
        results: list[dict] = []
        with self._lock:
            for rel_path, fm in self._index.items():
                if path_prefix and not rel_path.startswith(path_prefix):
                    continue
                if match_type == "exists":
                    if field in fm:
                        results.append({"path": rel_path, "frontmatter": fm})
                elif match_type == "exact":
                    if field in fm and str(fm[field]) == value:
                        results.append({"path": rel_path, "frontmatter": fm})
                elif match_type == "contains":
                    if field in fm and value.lower() in str(fm[field]).lower():
                        results.append({"path": rel_path, "frontmatter": fm})
        return results

    # ── LinkIndex queries ──────────────────────────────────────────────────────

    def get_backlinks(self, path: str) -> set[str]:
        """Return vault-relative paths of notes that link TO `path`.

        O(1) lookup — no filesystem scan.
        """
        with self._lock:
            return set(self._backlinks.get(path, set()))

    def get_forward_links(self, path: str) -> set[str]:
        """Return vault-relative paths that `path` links to.

        O(1) lookup — no filesystem scan.
        """
        with self._lock:
            return set(self._forward_links.get(path, set()))

    def rename_in_graph(self, old_path: str, new_path: str) -> None:
        """Update the link graph after a file has been moved/renamed.

        Called by vault_move after the filesystem move is complete.
        Does NOT rewrite the .md files — that's done by vault_move itself.
        """
        with self._lock:
            # Rename forward_links entry
            fwd = self._forward_links.pop(old_path, set())
            if new_path:
                self._forward_links[new_path] = fwd

            # Rename backlinks entry
            bl = self._backlinks.pop(old_path, set())
            if new_path:
                self._backlinks[new_path] = bl

            # Update all forward_links sets that pointed to old_path
            for src, targets in self._forward_links.items():
                if old_path in targets:
                    targets.discard(old_path)
                    if new_path:
                        targets.add(new_path)

            # Update all backlinks sets
            for tgt, sources in self._backlinks.items():
                if old_path in sources:
                    sources.discard(old_path)
                    if new_path:
                        sources.add(new_path)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _is_excluded(self, path: Path) -> bool:
        """Check whether any path component is in config.EXCLUDED_DIRS."""
        return bool(config.EXCLUDED_DIRS & set(path.relative_to(config.VAULT_PATH).parts))

    def _parse_frontmatter(self, path: Path) -> dict | None:
        """Parse YAML frontmatter from a markdown file. Returns None on failure."""
        try:
            post = frontmatter.load(str(path))
            return dict(post.metadata)
        except Exception:
            logger.warning("Failed to parse frontmatter: %s", path)
            return None

    def _update_links_for(
        self, rel: str, content: str, all_paths: set[str] | None = None
    ) -> None:
        """Parse links from `content` and update the graph for file `rel`.

        Thread-safe: acquires self._lock internally.
        """
        if all_paths is None:
            with self._lock:
                all_paths = set(self._index.keys()) | set(self._forward_links.keys())

        raw_links = _parse_links(content)
        resolved: set[str] = set()
        for raw in raw_links:
            target = _resolve_link(raw, rel, all_paths)
            if target:
                resolved.add(target)

        with self._lock:
            # Remove stale backlinks from old forward_links of this file
            old_targets = self._forward_links.get(rel, set())
            for old_tgt in old_targets - resolved:
                self._backlinks.setdefault(old_tgt, set()).discard(rel)

            # Add new backlinks
            for tgt in resolved - old_targets:
                self._backlinks.setdefault(tgt, set()).add(rel)

            self._forward_links[rel] = resolved

    def _remove_links_for(self, rel: str) -> None:
        """Remove all link graph entries for a deleted file."""
        with self._lock:
            old_targets = self._forward_links.pop(rel, set())
            for tgt in old_targets:
                self._backlinks.setdefault(tgt, set()).discard(rel)
            self._backlinks.pop(rel, None)

    def _schedule_debounce(self, abs_path: str) -> None:
        """Add a path to the pending set and reset the debounce timer."""
        with self._lock:
            self._pending_paths.add(abs_path)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                config.FRONTMATTER_INDEX_DEBOUNCE, self._flush_pending
            )
            self._debounce_timer.start()

    def _flush_pending(self) -> None:
        """Process all pending file changes (frontmatter + links)."""
        with self._lock:
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._debounce_timer = None

        for abs_path_str in paths:
            abs_path = Path(abs_path_str)
            rel = str(abs_path.relative_to(config.VAULT_PATH))
            if abs_path.exists():
                fm = self._parse_frontmatter(abs_path)
                with self._lock:
                    if fm is not None:
                        self._index[rel] = fm
                    else:
                        self._index.pop(rel, None)
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                    self._update_links_for(rel, content)
                except OSError:
                    pass
            else:
                with self._lock:
                    self._index.pop(rel, None)
                self._remove_links_for(rel)


class _VaultEventHandler(FileSystemEventHandler):
    """Watchdog handler that feeds .md changes into the frontmatter + link index."""

    def __init__(self, index: FrontmatterIndex) -> None:
        super().__init__()
        self._index = index

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".md":
            return
        if self._index._is_excluded(path):
            return
        self._index._schedule_debounce(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event)
