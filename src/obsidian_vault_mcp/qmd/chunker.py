"""Markdown chunker for QMD-Lite.

Splits .md files into semantically meaningful chunks that respect
heading boundaries and stay within the embedding model's token limit.

Strategy:
  1. Split on ## / ### headers (primary boundary — highest semantic value)
  2. If a section exceeds MAX_CHARS, sub-split on blank lines (paragraphs)
  3. Each chunk carries: file path, title, header path, char offset, raw text

No LLM or external call needed — this is pure CPU/string work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Approximate character limit per chunk.
# text-embedding-005 supports 2048 tokens; ~4 chars/token → ~8000 chars.
# We target ~3200 chars (≈800 tokens) to leave room for overlap and metadata.
MAX_CHARS = 3200
OVERLAP_CHARS = 200  # trailing context carried to next chunk


@dataclass
class Chunk:
    file_path: str        # vault-relative path, e.g. "agents/note.md"
    doc_title: str        # first H1 or filename stem
    header_path: str      # "## Section / ### Subsection" breadcrumb
    chunk_index: int      # 0-based index within the file
    char_offset: int      # starting char position in the original file
    text: str             # raw chunk text (including its leading header)
    embedding: list[float] = field(default_factory=list)


def _extract_title(content: str, file_path: str) -> str:
    """Return first H1 heading, or filename stem as fallback."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return Path(file_path).stem


def _split_by_headers(content: str) -> list[tuple[str, str, int]]:
    """Split content into (header_path, section_text, char_offset) tuples.

    Each element starts at a ## or deeper heading.
    The preamble before the first heading (if any) is returned with an
    empty header_path.
    """
    # Regex: match lines that start with ##+ (but not single #, which is title)
    header_re = re.compile(r"^(#{2,6})\s+(.+)$", re.MULTILINE)

    sections: list[tuple[str, str, int]] = []
    header_stack: list[str] = []  # track nested headings

    pos = 0
    prev_start = 0
    prev_header_path = ""

    for m in header_re.finditer(content):
        # Save the text from prev heading up to this one
        section_text = content[prev_start : m.start()]
        if section_text.strip():
            sections.append((prev_header_path, section_text, prev_start))

        depth = len(m.group(1))  # number of #
        name = m.group(2).strip()

        # Trim the stack to the current depth
        # depth 2 → index 0, depth 3 → index 1, …
        header_stack = header_stack[: depth - 2]
        header_stack.append(name)
        prev_header_path = " / ".join(header_stack)
        prev_start = m.start()

    # Remainder after last header
    tail = content[prev_start:]
    if tail.strip():
        sections.append((prev_header_path, tail, prev_start))

    return sections


def _sub_split(text: str, max_chars: int, overlap: int) -> list[tuple[str, int]]:
    """Split text that's too long into paragraph-aware sub-chunks.

    Returns list of (sub_text, relative_char_offset).
    """
    if len(text) <= max_chars:
        return [(text, 0)]

    # Try splitting on double newlines (paragraph boundaries)
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[tuple[str, int]] = []
    current = ""
    current_offset = 0
    abs_offset = 0

    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            # Flush current
            if current:
                chunks.append((current, current_offset))
                # Carry overlap from tail of current chunk
                overlap_text = current[-overlap:]
                current = overlap_text + "\n\n" + para
                current_offset = abs_offset - len(overlap_text)
            else:
                # Single paragraph larger than max_chars — hard split
                for i in range(0, len(para), max_chars - overlap):
                    sub = para[i : i + max_chars]
                    chunks.append((sub, abs_offset + i))
                current = ""
                current_offset = abs_offset + len(para)
        abs_offset += len(para) + 2  # account for \n\n separator

    if current:
        chunks.append((current, current_offset))

    return chunks if chunks else [(text, 0)]


def chunk_file(file_path: str, content: str) -> list[Chunk]:
    """Return all chunks for a single markdown file.

    Args:
        file_path: vault-relative path (used for metadata only)
        content:   full file content as string
    """
    doc_title = _extract_title(content, file_path)
    sections = _split_by_headers(content)

    if not sections:
        # Empty or no-header file — treat the whole thing as one chunk
        text = content.strip()
        if not text:
            return []
        return [Chunk(
            file_path=file_path,
            doc_title=doc_title,
            header_path="",
            chunk_index=0,
            char_offset=0,
            text=text,
        )]

    chunks: list[Chunk] = []
    idx = 0

    for header_path, section_text, base_offset in sections:
        for sub_text, rel_offset in _sub_split(section_text, MAX_CHARS, OVERLAP_CHARS):
            sub_text = sub_text.strip()
            if not sub_text:
                continue
            chunks.append(Chunk(
                file_path=file_path,
                doc_title=doc_title,
                header_path=header_path,
                chunk_index=idx,
                char_offset=base_offset + rel_offset,
                text=sub_text,
            ))
            idx += 1

    return chunks
