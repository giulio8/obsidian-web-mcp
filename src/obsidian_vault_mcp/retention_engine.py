"""Retention Engine — Ebbinghaus decay scoring for vault notes.

Implements the temporal decay + reinforcement boost formula from agentmemory:

    score = min(1, salience × e^(-λ × Δt_days) + reinforcement_boost)

Where:
    salience            = type_weight(frontmatter) + access_bonus(count)
    temporal_decay      = e^(-λ × days_since_creation)
    reinforcement_boost = σ × Σ(1 / days_since_access_i)  for recent access_i

Three tiers classify notes for observability:
    hot  (score ≥ 0.70) — actively used, high relevance
    warm (score ≥ 0.40) — recent but decaying
    cold (score ≥ 0.15) — stale, low but non-zero relevance
    evictable (score < 0.15) — candidate for archival

Parameters default to agentmemory's calibrated values:
    λ = 0.01  — half-life ≈ 69 days (slow decay for a knowledge base)
    σ = 0.30  — reinforcement weight per recent access

The engine is stateless and pure: given the same inputs, it always
produces the same output. AccessTracker provides the timestamps.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .access_tracker import AccessStats

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DecayConfig:
    """Ebbinghaus decay parameters.

    Attributes:
        lambda_: Decay rate (λ). Higher → faster forgetting. Default 0.01.
        sigma:   Reinforcement weight (σ). Higher → access boosts score more.
        tier_hot:       Score threshold to be classified as 'hot'.
        tier_warm:      Score threshold to be classified as 'warm'.
        tier_cold:      Score threshold to be classified as 'cold' (vs evictable).
    """
    lambda_: float = 0.01
    sigma: float = 0.30
    tier_hot: float = 0.70
    tier_warm: float = 0.40
    tier_cold: float = 0.15

    def __post_init__(self) -> None:
        if self.lambda_ <= 0:
            raise ValueError("lambda_ must be positive")
        if self.sigma < 0:
            raise ValueError("sigma must be non-negative")
        if not (self.tier_hot >= self.tier_warm >= self.tier_cold >= 0):
            raise ValueError("tier thresholds must satisfy hot >= warm >= cold >= 0")


DEFAULT_CONFIG = DecayConfig()

# Salience weights by frontmatter `type` field (mirrors agentmemory typeWeights)
_TYPE_SALIENCE: dict[str, float] = {
    "architecture":  0.90,
    "pattern":       0.80,
    "preference":    0.85,
    "concept":       0.75,
    "workflow":      0.60,
    "fact":          0.50,
    "reference":     0.45,
    "episodic":      0.35,
    "directory-readme": 0.10,
}
_DEFAULT_SALIENCE = 0.50


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────

class MemoryTier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    EVICTABLE = "evictable"


@dataclass
class RetentionScore:
    """Retention score for a single vault path."""
    path: str
    score: float              # Final clamped score [0, 1]
    salience: float           # Type weight + access bonus
    temporal_decay: float     # e^(-λ × Δt)
    reinforcement_boost: float
    access_count: int
    days_since_creation: float | None
    tier: MemoryTier
    config: DecayConfig = field(default_factory=lambda: DEFAULT_CONFIG, repr=False)

    @property
    def as_rrf_boost(self) -> float:
        """Convert retention score to a small additive RRF bonus.

        Scales the already-clamped [0,1] retention score into a [0, 0.10]
        range so it nudges ranking without overpowering query relevance.
        The score is already min(1, …), so this is guaranteed ≤ 0.10.
        """
        return self.score * 0.10


# ──────────────────────────────────────────────────────────────────────────────
# Core computation (pure functions)
# ──────────────────────────────────────────────────────────────────────────────

def _compute_salience(
    note_type: str | None,
    access_count: int,
) -> float:
    """Compute salience from frontmatter type and access history.

    access_bonus caps at 0.20 (10 accesses × 0.02).
    """
    base = _TYPE_SALIENCE.get(note_type or "", _DEFAULT_SALIENCE)
    access_bonus = min(0.20, access_count * 0.02)
    return min(1.0, base + access_bonus)


def _compute_reinforcement_boost(
    access_timestamps: list[float],
    sigma: float,
) -> float:
    """Sum of σ/days_since_access for each recent access timestamp.

    Implements: boost = σ × Σ(1 / Δt_i) where Δt_i > 0.

    Timestamps are unix floats (time.time() format).
    Very recent accesses (< 1 hour ago, Δt < 0.042 days) are clamped
    to avoid extreme boosts.
    """
    now = time.time()
    MIN_DAYS = 1.0 / 24.0  # 1 hour minimum to prevent division spikes
    boost = 0.0
    for ts in access_timestamps:
        if not math.isfinite(ts):
            continue
        days_since = (now - ts) / 86400.0
        if days_since > 0:
            boost += 1.0 / max(days_since, MIN_DAYS)
    return boost * sigma


def _classify_tier(score: float, config: DecayConfig) -> MemoryTier:
    if score >= config.tier_hot:
        return MemoryTier.HOT
    elif score >= config.tier_warm:
        return MemoryTier.WARM
    elif score >= config.tier_cold:
        return MemoryTier.COLD
    else:
        return MemoryTier.EVICTABLE


def compute_retention(
    path: str,
    note_type: str | None,
    created_at_ts: float | None,
    access_stats: "AccessStats | None",
    config: DecayConfig = DEFAULT_CONFIG,
) -> RetentionScore:
    """Compute the Ebbinghaus retention score for a single vault note.

    Args:
        path:           Vault-relative path (for the result).
        note_type:      Frontmatter `type` field (e.g. 'concept', 'reference').
        created_at_ts:  Unix timestamp of file creation. None → assume new (Δt=0).
        access_stats:   AccessStats from AccessTracker. None → zero access history.
        config:         Decay parameters. Defaults to DEFAULT_CONFIG.

    Returns:
        RetentionScore dataclass with all intermediate values.
    """
    now = time.time()

    # Access history
    access_count = access_stats.total_count if access_stats else 0
    recent_ts = access_stats.recent_timestamps if access_stats else []

    # Salience
    salience = _compute_salience(note_type, access_count)

    # Temporal decay: e^(-λ × Δt_days)
    if created_at_ts is not None and math.isfinite(created_at_ts):
        delta_days = max(0.0, (now - created_at_ts) / 86400.0)
    else:
        delta_days = 0.0
    temporal_decay = math.exp(-config.lambda_ * delta_days)

    # Reinforcement boost from recent accesses
    reinforcement_boost = _compute_reinforcement_boost(recent_ts, config.sigma)

    # Final score
    raw_score = salience * temporal_decay + reinforcement_boost
    score = min(1.0, raw_score)

    return RetentionScore(
        path=path,
        score=score,
        salience=salience,
        temporal_decay=temporal_decay,
        reinforcement_boost=reinforcement_boost,
        access_count=access_count,
        days_since_creation=delta_days if created_at_ts is not None else None,
        tier=_classify_tier(score, config),
        config=config,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Batch scoring (for bulk operations like stale-path reporting)
# ──────────────────────────────────────────────────────────────────────────────

def compute_retention_batch(
    notes: list[dict],
    access_map: "dict[str, AccessStats]",
    config: DecayConfig = DEFAULT_CONFIG,
) -> list[RetentionScore]:
    """Compute retention scores for a batch of notes.

    Args:
        notes:      List of dicts with at minimum {'path': str}. May include
                    'type' (str) and 'created_at_ts' (float unix timestamp).
        access_map: Dict mapping path → AccessStats from AccessTracker.
        config:     Decay parameters.

    Returns:
        List of RetentionScore sorted by score descending.
    """
    results = []
    for note in notes:
        path = note.get("path", "")
        rs = compute_retention(
            path=path,
            note_type=note.get("type"),
            created_at_ts=note.get("created_at_ts"),
            access_stats=access_map.get(path),
            config=config,
        )
        results.append(rs)
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def tier_summary(scores: list[RetentionScore]) -> dict:
    """Return a count breakdown by tier."""
    counts: dict[str, int] = {t.value: 0 for t in MemoryTier}
    for s in scores:
        counts[s.tier.value] += 1
    return counts
