"""Replay a frozen case through the real ranking path and score it.

What runs here is deliberately *the same code* the daily job runs
(:func:`ai_digest.process.rank.enrich_articles` plus the same heuristic
prefilter). An eval that reimplements the pipeline measures the eval, not the
pipeline.

Candidates that the prefilter drops still receive their heuristic score, so
they can appear in the ranking -- just low. That mirrors production, where they
are never published at all.
"""

from __future__ import annotations

from typing import Any

from ..llm.base import LLMClient, build_llm
from ..models import Article, utcnow
from ..pipeline import heuristic_score
from ..process.normalize import canonicalize_url, content_hash
from ..process.rank import enrich_articles
from ..process.simhash import simhash, to_hex
from .dataset import EvalCase, EvalLabels
from .metrics import evaluate_ranking


def articles_from_case(case: EvalCase) -> list[Article]:
    articles: list[Article] = []
    for item in case.items:
        text = f"{item.title} {item.summary}".strip()
        articles.append(
            Article(
                source=item.source,
                url=item.id,
                title=item.title,
                summary=item.summary,
                canonical_url=canonicalize_url(item.id),
                content_hash=content_hash(text),
                simhash_hex=to_hex(simhash(text)),
            )
        )
    return articles


async def evaluate_case(
    case: EvalCase,
    labels: EvalLabels,
    settings,
    *,
    llm: LLMClient | None = None,
    k: int | None = None,
) -> dict[str, Any]:
    """Run the ranker over a frozen case; return metrics and the full ranking."""
    llm = llm or build_llm(settings)
    k = k or settings.top_n
    articles = articles_from_case(case)

    shortlist = sorted(
        articles, key=lambda a: heuristic_score(a), reverse=True
    )[: settings.prefilter_n]

    scores, llm_stats = await enrich_articles(
        shortlist,
        llm,
        batch_size=settings.llm_batch_size,
        concurrency=settings.llm_concurrency,
    )

    combined: dict[str, float] = {
        article.canonical_url: heuristic_score(article) for article in articles
    }
    combined.update({item_id: score.score for item_id, score in scores.items()})

    ranked = sorted(
        articles, key=lambda a: combined[a.canonical_url], reverse=True
    )
    ranked_ids = [a.canonical_url for a in ranked]

    metrics = evaluate_ranking(ranked_ids, labels.labels, k=k, scores=combined)

    usage = getattr(llm, "usage", {}) or {}
    return {
        "case_id": case.case_id,
        "provider": getattr(llm, "name", "unknown"),
        "generated_at": utcnow().isoformat(),
        "k": k,
        "label_coverage": labels.coverage(case),
        "labeler": labels.labeler,
        "config": {
            "prefilter_n": settings.prefilter_n,
            "top_n": settings.top_n,
            "llm_batch_size": settings.llm_batch_size,
            "deepseek_model": settings.deepseek_model,
        },
        "llm_stats": llm_stats,
        "tokens": {
            "prompt": usage.get("prompt_tokens", 0),
            "completion": usage.get("completion_tokens", 0),
        },
        "metrics": metrics,
        "ranking": [
            {
                "rank": index + 1,
                "id": article.canonical_url,
                "title": article.title,
                "source": article.source,
                "score": combined[article.canonical_url],
                "label": labels.labels.get(article.canonical_url),
                "one_liner": scores[article.canonical_url].one_liner
                if article.canonical_url in scores
                else "",
            }
            for index, article in enumerate(ranked)
        ],
    }


def compare(report: dict, baseline: dict) -> dict[str, dict[str, float]]:
    """Per-metric deltas, so 'did it get better' is a number, not a feeling."""
    keys = [k for k in report["metrics"] if k in baseline["metrics"]]
    return {
        key: {
            "baseline": baseline["metrics"][key],
            "current": report["metrics"][key],
            "delta": round(report["metrics"][key] - baseline["metrics"][key], 4),
        }
        for key in keys
        if isinstance(report["metrics"][key], (int, float))
    }
