"""Vertex AI + Gemini client for QMD-Lite.

Wraps two Google Cloud APIs:
  - Vertex AI text-embedding-005  → float vectors for semantic search
  - Gemini 2.0 Flash              → query expansion (Phase 3)

Authentication: uses Application Default Credentials (ADC).
On the GCP VM, the service account attached to the instance is used
automatically — no explicit key file needed.

Usage on VM setup:
  gcloud auth application-default login   # only for local dev
  # On VM: nothing needed, ADC picks up the SA automatically

Cost reminder:
  text-embedding-005: $0.006 / 1M tokens
  gemini-2.0-flash:   ~$0.10 / 1M output tokens
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

logger = logging.getLogger(__name__)

# Pulled from env (set in .env or systemd EnvironmentFile)
_PROJECT = os.environ.get("GCP_PROJECT_ID", "")
_REGION = os.environ.get("GCP_REGION", "us-east1")
_EMBED_MODEL = "text-embedding-005"
_CHAT_MODEL = "gemini-2.0-flash"

# Embedding dimensions for text-embedding-005
EMBED_DIM = 768

# Vertex AI batch limit
_MAX_BATCH = 250


def _get_embed_client():
    """Lazy-load the Vertex AI prediction client."""
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel

        if not _PROJECT:
            raise EnvironmentError(
                "GCP_PROJECT_ID not set. Add it to .env or export it."
            )
        vertexai.init(project=_PROJECT, location=_REGION)
        return TextEmbeddingModel.from_pretrained(_EMBED_MODEL)
    except ImportError as e:
        raise ImportError(
            "google-cloud-aiplatform not installed. "
            "Run: uv add google-cloud-aiplatform"
        ) from e


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return embeddings for a list of texts.

    Handles Vertex AI batch limit (250 per call) transparently.
    Each text is trimmed to ~8000 chars to stay within the 2048 token limit.

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

    model = _get_embed_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(trimmed), _MAX_BATCH):
        batch = trimmed[i : i + _MAX_BATCH]
        try:
            results = model.get_embeddings(batch)
            all_embeddings.extend(r.values for r in results)
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
        import vertexai
        from vertexai.generative_models import GenerativeModel

        if not _PROJECT:
            return [query]

        vertexai.init(project=_PROJECT, location=_REGION)
        model = GenerativeModel(_CHAT_MODEL)

        prompt = (
            "Generate 2 alternative phrasings of the following search query "
            "that capture the same information need but use different words. "
            "Output ONLY the two alternatives, one per line, no numbering, no explanation.\n\n"
            f"Query: {query}"
        )

        response = model.generate_content(prompt)
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
