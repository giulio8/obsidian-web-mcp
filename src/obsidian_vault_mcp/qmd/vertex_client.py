"""Vertex AI + Gemini client for QMD-Lite (google-genai SDK).

Wraps two Google Cloud APIs via the new `google-genai` SDK:
  - text-embedding-005  → float vectors for semantic search
  - gemini-2.0-flash    → query expansion (Phase 3)

The `google-cloud-aiplatform` SDK's vertexai.language_models module was
deprecated on June 24, 2025. This module uses the replacement SDK:
  pip install google-genai

Authentication: uses Application Default Credentials (ADC).
On the GCP VM, the service account attached to the instance is used
automatically — no explicit key file needed.

Cost reminder:
  text-embedding-005: $0.006 / 1M tokens
  gemini-2.0-flash:   ~$0.10 / 1M output tokens
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Pulled from env (set in .env or systemd EnvironmentFile)
_PROJECT = os.environ.get("GCP_PROJECT_ID", "")
_REGION = os.environ.get("GCP_REGION", "us-east1")
_EMBED_MODEL = "text-embedding-005"
_CHAT_MODEL = "gemini-2.5-flash"

# Embedding dimensions for text-embedding-005
EMBED_DIM = 768

# Vertex AI batch limit (250 texts, 20k tokens total)
_MAX_BATCH = 20  # conservative to stay under 20k total tokens


def _get_genai_client():
    """Lazy-load the google-genai client configured for Vertex AI."""
    try:
        from google import genai  # type: ignore

        if not _PROJECT:
            raise EnvironmentError(
                "GCP_PROJECT_ID not set. Add it to .env or export it."
            )
        return genai.Client(vertexai=True, project=_PROJECT, location=_REGION)
    except ImportError as e:
        raise ImportError(
            "google-genai not installed. Run: uv add google-genai"
        ) from e


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return embeddings for a list of texts.

    Handles Vertex AI batch limit transparently.
    Each text is trimmed to ~750 tokens (3000 chars) to stay within
    the 20k token-per-batch limit when batching 20 texts.

    Args:
        texts: list of strings to embed

    Returns:
        list of float vectors, one per input text
    """
    if not texts:
        return []

    # Trim to safe length.
    # text-embedding-005 supports 3072 tokens/text, but we cap at ~750 tokens
    # (3000 chars) so a batch of 20 texts stays well under the 20k total limit.
    trimmed = [t[:3000] for t in texts]

    client = _get_genai_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(trimmed), _MAX_BATCH):
        batch = trimmed[i : i + _MAX_BATCH]
        try:
            response = client.models.embed_content(
                model=_EMBED_MODEL,
                contents=batch,
            )
            all_embeddings.extend(e.values for e in response.embeddings)
            logger.debug(f"Embedded batch {i//_MAX_BATCH + 1}: {len(batch)} texts")
        except Exception as e:
            logger.error(f"Embedding batch {i} failed: {e}")
            # Fill with zero vectors to keep indices aligned
            all_embeddings.extend([[0.0] * EMBED_DIM] * len(batch))

    return all_embeddings


def embed_query(query: str) -> list[float]:
    """Embed a single query string. Convenience wrapper around embed_texts."""
    results = embed_texts([query])
    return results[0] if results else [0.0] * EMBED_DIM


def route_query(query: str, vault_schema: str) -> "list":
    """Decompose a user query into targeted vault sub-searches via Gemini Flash.

    Instead of generating paraphrases, the SLM acts as a vault router: it reads
    the vault directory structure and decomposes the query into 1-3 SubQuery
    objects, each potentially scoped to a specific vault path prefix.

    Args:
        query:        Original user query (any language)
        vault_schema: Structured vault schema from FrontmatterIndex.get_vault_schema()

    Returns:
        list[SubQuery] - always at least one element (fallback = global sweep).
    """
    from .search_engine import SubQuery
    import json as _json

    fallback = [SubQuery(query=query, path_prefix=None, weight=2.0)]

    try:
        client = _get_genai_client()

        prompt = (
            "You are the routing brain of a personal knowledge base search system.\n"
            "Your task is to DECOMPOSE the user query into 1-3 targeted sub-searches,\n"
            "each aimed at the correct section of the vault. Do NOT rephrase or\n"
            "generate synonyms - focus exclusively on routing.\n\n"
            f"{vault_schema}\n\n"
            f'USER QUERY: "{query}"\n\n'
            "ROUTING RULES:\n"
            "- Use path_prefix only when the query clearly targets a specific vault section.\n"
            "- ALWAYS include at least one sub-query with path_prefix=null as a global fallback.\n"
            "- For temporal queries (last week, yesterday, recent, questa settimana, etc.),\n"
            "  route to the Timeline path shown above for the relevant time period.\n"
            "- For project/work queries, route to Editors/Giulio/ or Knowledge/Reply/.\n"
            "- Queries may be in Italian or English - route based on meaning, not language.\n"
            "- weight=2.0 for the most targeted sub-search, weight=1.0 for supporting ones.\n\n"
            "Output ONLY valid JSON (no markdown, no explanation outside JSON):\n"
            '{"sub_queries": [{"query": "...", "path_prefix": "..." or null, "weight": 2.0 or 1.0}]}'
        )

        response = client.models.generate_content(
            model=_CHAT_MODEL,
            contents=prompt,
        )

        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        data = _json.loads(raw)
        parsed = []
        for item in data.get("sub_queries", []):
            q = str(item.get("query", "")).strip()
            if not q:
                continue
            parsed.append(SubQuery(
                query=q,
                path_prefix=item.get("path_prefix") or None,
                weight=float(item.get("weight", 1.0)),
            ))

        if not parsed:
            logger.warning("route_query: SLM returned no valid sub_queries, using fallback")
            return fallback

        # Ensure there is always at least one global (unprefixed) sub-query
        has_global = any(sq.path_prefix is None for sq in parsed)
        if not has_global:
            parsed.append(SubQuery(query=query, path_prefix=None, weight=1.0))

        logger.debug(f"route_query: {query!r} -> {[(sq.query, sq.path_prefix) for sq in parsed]}")
        return parsed

    except Exception as e:
        logger.warning(f"route_query failed, using global fallback: {e}")
        return fallback


def rerank_chunks(query: str, chunks: list[str]) -> list[float]:
    """Score each chunk for relevance to the query via Gemini Flash.

    Asks the model to rate each (query, chunk) pair on a 0-1 scale.
    This is the optional reranking step — only call it when the agent
    signals that the query is complex or ambiguous.

    Args:
        query:  the original user query
        chunks: list of chunk texts to score (typically top-30 from retrieval)

    Returns:
        list of float scores in [0, 1], parallel to the input chunks list.
        Falls back to uniform 0.5 on error to avoid discarding results.
    """
    if not chunks:
        return []

    try:
        client = _get_genai_client()

        # Build a single prompt that scores all chunks in one call (cheaper)
        items = "\n\n".join(
            f"[{i}] {chunk[:800]}"  # trim each chunk to keep prompt manageable
            for i, chunk in enumerate(chunks)
        )

        prompt = (
            f"Query: {query}\n\n"
            "For each numbered text chunk below, output a single decimal score "
            "between 0.0 and 1.0 indicating how directly relevant it is to the query.\n"
            "1.0 = answers the query directly, 0.0 = completely irrelevant.\n"
            "Output ONLY a JSON array of numbers, one per chunk, in the same order. "
            "No explanation.\n\n"
            f"{items}"
        )

        response = client.models.generate_content(
            model=_CHAT_MODEL,
            contents=prompt,
        )

        import json
        raw = response.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw.strip())

        if not isinstance(scores, list) or len(scores) != len(chunks):
            raise ValueError(f"Unexpected reranker output: {scores!r}")

        # Clamp to [0, 1]
        return [max(0.0, min(1.0, float(s))) for s in scores]

    except Exception as e:
        logger.warning(f"Reranking failed: {e}. Falling back to uniform score.")
        return [0.5] * len(chunks)
