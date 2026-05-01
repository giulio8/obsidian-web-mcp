"""Hybrid search engine for QMD-Lite.

Implements the full retrieval pipeline:
  1. BM25 keyword search  (SQLite FTS5)
  2. Vector semantic search (sqlite-vec, cosine similarity)
  3. RRF Fusion with top-rank bonus
  4. Position-aware blending with optional reranking (Phase 3)

Design follows tobi/qmd fusion strategy:
  - RRF k=60 (standard)
  - Original query gets ×2 weight vs expanded variants
  - Documents ranking #1 in any list get +0.05 bonus, #2-3 get +0.02
  - Position-aware blend: top 1-3 trust retrieval more, lower ranks trust reranker more

Usage:
    engine = HybridSearchEngine(db)
    results = engine.search("come funzionano i Transformer?", top_k=5)
    for r in results:
        print(r.score, r.doc_path, r.snippet)
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from .db import QMDDatabase

logger = logging.getLogger(__name__)

# RRF constant (60 is standard from the original RRF paper)
RRF_K = 60

# Top-rank bonuses to preserve exact matches against expanded queries
RANK_BONUS = {0: 0.05, 1: 0.02, 2: 0.02}  # 0-indexed rank → bonus

# How many candidates to collect before applying reranker
RERANK_CANDIDATES = 30


@dataclass
class SearchResult:
    """A single result from the hybrid search pipeline."""
    chunk_id: int
    doc_path: str
    doc_title: str
    header_path: str
    char_offset: int
    text: str
    score: float                  # final blended score (0-1)
    rrf_score: float = 0.0        # raw RRF score before blending
    rerank_score: float = 0.0     # reranker score (0-1), 0 if not reranked
    sources: list[str] = field(default_factory=list)  # which backends found this

    @property
    def snippet(self) -> str:
        """Return a short preview of the chunk text (first 300 chars)."""
        return self.text[:300].strip() + ("…" if len(self.text) > 300 else "")

    @property
    def obsidian_link(self) -> str:
        """Return Obsidian wiki-link format: [[path/to/file]]"""
        stem = self.doc_path.removesuffix(".md")
        return f"[[{stem}]]"


class HybridSearchEngine:
    """Combines BM25 and vector search via RRF for hybrid retrieval."""

    def __init__(self, db: QMDDatabase):
        self.db = db

    # ── Public API ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        queries: list[str] | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        rerank_fn: Callable[[str, list[str]], list[float]] | None = None,
        bm25_limit: int = 30,
        vector_limit: int = 30,
    ) -> list[SearchResult]:
        """Perform hybrid BM25 + vector search with RRF fusion.

        Args:
            query:        Primary query string (always weighted ×2 in RRF)
            top_k:        Number of results to return
            queries:      Additional query variants (e.g. from query expansion).
                          If None, only the primary query is used.
            embed_fn:     Function(text) → vector. If None, vector search is skipped.
            rerank_fn:    Function(query, [texts]) → [score]. Optional reranker.
            bm25_limit:   How many BM25 candidates to gather per query variant
            vector_limit: How many vector candidates to gather per query variant

        Returns:
            List of SearchResult sorted by descending score, length ≤ top_k
        """
        all_queries = [query] + (queries or [])

        # Collect ranked lists from each backend for each query variant
        ranked_lists: list[tuple[list[dict], float]] = []  # (results, weight)

        for i, q in enumerate(all_queries):
            weight = 2.0 if i == 0 else 1.0  # primary query gets double weight

            # BM25 search
            bm25_results = self._safe_bm25(q, bm25_limit)
            if bm25_results:
                ranked_lists.append((bm25_results, weight))
                logger.debug(f"BM25[{i}] '{q[:40]}': {len(bm25_results)} results")

            # Vector search (only if embedding function provided)
            if embed_fn is not None:
                try:
                    vec = embed_fn(q)
                    vector_results = self.db.vector_search(vec, vector_limit)
                    if vector_results:
                        ranked_lists.append((vector_results, weight))
                        logger.debug(
                            f"Vec[{i}] '{q[:40]}': {len(vector_results)} results"
                        )
                except Exception as e:
                    logger.warning(f"Vector search failed for query {i}: {e}")

        if not ranked_lists:
            logger.warning("No results from any backend")
            return []

        # RRF Fusion
        fused = self._rrf_fuse(ranked_lists)

        # Take top RERANK_CANDIDATES before (optional) reranking
        candidates = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)
        candidates = candidates[:RERANK_CANDIDATES]

        # Optional reranking
        if rerank_fn is not None and candidates:
            try:
                texts = [c["text"] for c in candidates]
                rerank_scores = rerank_fn(query, texts)
                for c, rs in zip(candidates, rerank_scores):
                    c["rerank_score"] = rs
            except Exception as e:
                logger.warning(f"Reranking failed: {e}")

        # Position-aware blending and final sort
        results = [self._blend(c, rank) for rank, c in enumerate(candidates)]
        results.sort(key=lambda r: r.score, reverse=True)

        return results[:top_k]

    def bm25_only(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """Fast BM25-only search, no embedding needed."""
        raw = self._safe_bm25(query, top_k)
        return [
            SearchResult(
                chunk_id=r["chunk_id"],
                doc_path=r["doc_path"],
                doc_title=r["doc_title"],
                header_path=r["header_path"],
                char_offset=r.get("char_offset", 0),
                text=r["text"],
                score=r["score"],
                rrf_score=r["score"],
                sources=["bm25"],
            )
            for r in raw
        ]

    # ── RRF Fusion ────────────────────────────────────────────────────────────

    def _rrf_fuse(
        self, ranked_lists: list[tuple[list[dict], float]]
    ) -> dict[int, dict]:
        """Reciprocal Rank Fusion across multiple ranked result lists.

        Args:
            ranked_lists: list of (results, weight) pairs.
                          Each result dict must have a 'chunk_id' key.

        Returns:
            Dict mapping chunk_id → merged result dict with 'rrf_score'.
        """
        scores: dict[int, float] = {}
        merged: dict[int, dict] = {}

        for results, weight in ranked_lists:
            for rank, item in enumerate(results):
                cid = item["chunk_id"]
                rrf = weight / (RRF_K + rank + 1)

                # Top-rank bonus: preserves exact matches
                rrf += RANK_BONUS.get(rank, 0.0) * weight

                scores[cid] = scores.get(cid, 0.0) + rrf

                # Keep the best (highest score) version of the item
                if cid not in merged or item.get("score", 0) > merged[cid].get("score", 0):
                    merged[cid] = {**item, "sources": []}

                # Track which backends found this chunk
                source_tag = "bm25" if "score" in item and item["score"] > 0 else "vec"
                if source_tag not in merged[cid]["sources"]:
                    merged[cid]["sources"].append(source_tag)

        for cid in merged:
            merged[cid]["rrf_score"] = scores[cid]

        return merged

    # ── Blending ──────────────────────────────────────────────────────────────

    @staticmethod
    def _blend(item: dict, rank: int) -> SearchResult:
        """Position-aware blend of RRF score and reranker score.

        Mirrors tobi/qmd blending:
          - rank 0-2:  75% retrieval, 25% reranker  (trust retrieval for top hits)
          - rank 3-9:  60% retrieval, 40% reranker
          - rank 10+:  40% retrieval, 60% reranker  (reranker has more say lower down)

        If no reranker score, falls back to pure RRF (normalized 0-1).
        """
        rrf = item.get("rrf_score", 0.0)
        rerank = item.get("rerank_score", 0.0)

        # Normalize RRF to 0-1 range (max theoretical ≈ 2×1/61 ≈ 0.033 with weight=2)
        # We use a soft normalization so it stays comparable across vault sizes
        rrf_norm = rrf / (rrf + 0.05) if rrf > 0 else 0.0

        if rerank > 0:
            if rank <= 2:
                w_ret, w_rer = 0.75, 0.25
            elif rank <= 9:
                w_ret, w_rer = 0.60, 0.40
            else:
                w_ret, w_rer = 0.40, 0.60
            final = w_ret * rrf_norm + w_rer * rerank
        else:
            final = rrf_norm

        return SearchResult(
            chunk_id=item["chunk_id"],
            doc_path=item["doc_path"],
            doc_title=item.get("doc_title", ""),
            header_path=item.get("header_path", ""),
            char_offset=item.get("char_offset", 0),
            text=item["text"],
            score=final,
            rrf_score=rrf,
            rerank_score=rerank,
            sources=item.get("sources", []),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _safe_bm25(self, query: str, limit: int) -> list[dict]:
        """BM25 search with FTS5 special-char sanitisation.

        FTS5 treats several chars as operators: " * ^ - + ( ) AND OR NOT.
        Strip punctuation so we never hit a syntax error, then try a
        quoted phrase first, falling back to individual AND'd terms.
        """
        # Remove chars that FTS5 parses as operators (keep letters, digits, spaces)
        sanitised = re.sub(r'[^\w\s]', ' ', query, flags=re.UNICODE).strip()
        if not sanitised:
            return []

        try:
            # Quoted phrase search (exact word-order)
            results = self.db.bm25_search(f'"{sanitised}"', limit)
            if not results:
                # Fallback: AND of individual terms
                results = self.db.bm25_search(sanitised, limit)
            return results
        except Exception as e:
            logger.warning(f"BM25 search failed for '{query}': {e}")
            return []
