"""Two-stage project category detector.

The first stage is deterministic and cheap. The optional fallback callable is
used only when signals are ambiguous; tests and offline planning can omit it
and receive the safe general-web fallback.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from shamsu.registry.categories import SIGNALS
from shamsu.registry.schema import Category


@dataclass(frozen=True)
class CategoryDecision:
    category: Category
    confidence: float
    scores: dict[str, int] = field(default_factory=dict)
    reason: str = ""


def score_categories(text: str) -> dict[Category, int]:
    lowered = text.lower()
    scores = {category: 0 for category in Category}
    for category, signals in SIGNALS.items():
        for keyword, weight in signals:
            if keyword in lowered:
                scores[category] += weight
    return scores


def deterministic_pick(scores: dict[Category, int]) -> tuple[Category | None, float]:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top_score == 0:
        return None, 0.0
    margin = top_score - second
    confidence = min(1.0, (top_score + margin) / 10)
    if top_score >= 5 and margin >= 2:
        return top, confidence
    return None, confidence


def detect_category(
    text: str,
    fallback: Callable[[str, dict[Category, int]], CategoryDecision] | None = None,
) -> CategoryDecision:
    scores = score_categories(text)
    category, confidence = deterministic_pick(scores)
    serializable_scores = {category.value: score for category, score in scores.items()}
    if category is not None:
        return CategoryDecision(
            category=category,
            confidence=confidence,
            scores=serializable_scores,
            reason="deterministic category signals",
        )
    if fallback is not None:
        return fallback(text, scores)
    return CategoryDecision(
        category=Category.GENERAL_WEB,
        confidence=max(confidence, 0.35),
        scores=serializable_scores,
        reason="ambiguous or no category signals; using general-web fallback",
    )
