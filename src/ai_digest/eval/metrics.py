"""Ranking metrics for binary relevance.

Binary, not graded, on purpose: the labelling tool asks one yes/no question
("should an AI engineer read this?"). Graded relevance would be more expressive
but needs a rubric that two people can apply consistently, and this project has
exactly one labeller.

All functions take ``ranked_ids`` (best first) and ``labels`` (id -> 0/1).
"""

from __future__ import annotations

import math
from typing import Iterable


def _top(ranked_ids: Iterable[str], k: int) -> list[str]:
    return list(ranked_ids)[:k]


def precision_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    """Of the k items we would publish, how many are actually relevant."""
    top = _top(ranked_ids, k)
    if not top:
        return 0.0
    return sum(1 for item_id in top if labels.get(item_id, 0) == 1) / len(top)


def recall_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    """Of all relevant items in the pool, how many made it into the top k."""
    total_relevant = sum(1 for value in labels.values() if value == 1)
    if total_relevant == 0:
        return 0.0
    top = _top(ranked_ids, k)
    hits = sum(1 for item_id in top if labels.get(item_id, 0) == 1)
    return hits / total_relevant


def dcg(gains: list[float]) -> float:
    return sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))


def ndcg_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    """Normalised discounted cumulative gain; rewards putting relevant items early.

    Using binary gains with an ideal ordering, this is the standard NDCG@k. It
    differs from precision@k in one useful way: swapping rank 1 and rank 12
    matters, which is exactly the failure mode we care about (a great story
    buried at position 11 is nearly as bad as missing it).
    """
    top = _top(ranked_ids, k)
    if not top:
        return 0.0
    gains = [float(labels.get(item_id, 0)) for item_id in top]
    ideal = sorted(labels.values(), reverse=True)[:k]
    ideal_gains = [float(value) for value in ideal]
    best = dcg(ideal_gains)
    if best == 0:
        return 0.0
    return dcg(gains) / best


def mean_score_by_relevance(
    ranked_ids: list[str], scores: dict[str, float], labels: dict[str, int]
) -> dict[str, float]:
    """Sanity check: relevant items should score higher than irrelevant ones.

    If the two means are close, the ranking is working by luck rather than by
    signal, and precision@12 will be unstable across days.
    """
    relevant = [scores[i] for i in ranked_ids if labels.get(i) == 1]
    irrelevant = [scores[i] for i in ranked_ids if labels.get(i) == 0]
    return {
        "mean_score_relevant": round(sum(relevant) / len(relevant), 2) if relevant else 0.0,
        "mean_score_irrelevant": (
            round(sum(irrelevant) / len(irrelevant), 2) if irrelevant else 0.0
        ),
        "n_relevant": len(relevant),
        "n_irrelevant": len(irrelevant),
    }


def evaluate_ranking(
    ranked_ids: list[str],
    labels: dict[str, int],
    *,
    k: int,
    scores: dict[str, float] | None = None,
) -> dict:
    """All metrics for one ranked list, as a plain dict (JSON-serialisable)."""
    result: dict = {
        f"precision@{k}": round(precision_at_k(ranked_ids, labels, k), 4),
        f"recall@{k}": round(recall_at_k(ranked_ids, labels, k), 4),
        f"ndcg@{k}": round(ndcg_at_k(ranked_ids, labels, k), 4),
        "pool_size": len(ranked_ids),
        "labelled": len(labels),
        "relevant_total": sum(1 for v in labels.values() if v == 1),
    }
    if scores:
        result.update(mean_score_by_relevance(ranked_ids, scores, labels))
    return result
