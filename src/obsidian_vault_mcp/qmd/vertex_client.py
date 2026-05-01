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
_CHAT_MODEL = "gemini-2.0-flash"

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


def expand_query(query: str) -> list[str]:
    """Generate 1-2 alternative phrasings for the query via Gemini Flash.

    Used in Phase 3 (query expansion). Returns the original query plus
    alternatives. Falls back gracefully if the API fails.

    Args:
        query: original user query

    Returns:
        list of query strings (original always included as first element)
    """
    try:
        client = _get_genai_client()

        prompt = (
            "Generate 2 alternative phrasings of the following search query "
            "that capture the same information need but use different words. "
            "Output ONLY the two alternatives, one per line, no numbering, no explanation.\n\n"
            f"Query: {query}"
        )

        response = client.models.generate_content(
            model=_CHAT_MODEL,
            contents=prompt,
        )
        alternatives = [
            line.strip()
            for line in response.text.strip().splitlines()
            if line.strip() and line.strip().lower() != query.lower()
        ][:2]  # cap at 2

        logger.debug(f"Query expanded: {query!r} → {alternatives}")
        return [query] + alternatives

    except Exception as e:
        logger.warning(f"Query expansion failed, using original: {e}")
        return [query]
