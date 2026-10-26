"""Transparent reranking: all contributions are returned to the caller."""

import math

from .embeddings import tokens
from .models import Memory, RecallResult, Tier, now


def lexical_overlap(query: str, content: str) -> float:
    query_words = set(tokens(query))
    return len(query_words & set(tokens(content))) / max(1, len(query_words))


def rank(
    memory: Memory,
    semantic: float,
    lexical: float,
    entity_ids: list[str],
    contextual: float = 0,
    chain: bool = False,
    resurfaced: bool = False,
) -> RecallResult:
    age = max(0, (now() - memory.created_at).total_seconds() / 86400)
    contributions = {
        "semantic": 0.40 * semantic,
        "lexical": 0.25 * lexical,
        "importance": 0.08 * memory.importance,
        "confidence": 0.04 * memory.confidence,
        "recency": 0.04 * math.exp(-age / 365),
        "access_frequency": 0.03 * min(1, math.log1p(memory.access_count) / 5),
        "relationship": 0.07 * bool(set(entity_ids) & set(memory.entity_ids)),
        "tier": 0.04 if memory.tier == Tier.PRIORITY else 0,
        "context": 0.05 * contextual,
        "supersession": -0.2 if memory.superseded_by else 0,
        "chain": -0.03 if chain else 0,
    }
    return RecallResult(
        memory=memory,
        score=sum(contributions.values()),
        explanation={
            "semantic_score": semantic,
            "lexical_score": lexical,
            "contributions": contributions,
            "tier": memory.tier.value,
            "chain_expansion": chain,
            "resurfaced": resurfaced,
            "reasons": [key for key, value in contributions.items() if value > 0],
        },
    )
