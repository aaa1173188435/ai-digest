"""End-to-end orchestration.

The pipeline is deliberately a straight line -- fetch, persist, cluster,
enrich, render -- because a straight line is easy to reason about when it fails
at 06:00. Concurrency appears in exactly two places (network fetch, LLM
batches); everything that touches SQLite runs sequentially on one thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta

from . import db
from .ingest.fetcher import Fetcher
from .ingest.rss import parse_feed_lenient
from .ingest.sources import load_sources
from .llm.base import LLMClient, build_llm
from .llm.fake import heuristic_enrich
from .models import Article, Digest, DigestItem, utcnow
from .process.dedupe import Cluster, cluster_articles
from .process.normalize import canonicalize_url, content_hash
from .process.rank import enrich_articles
from .process.simhash import simhash, to_hex

log = logging.getLogger(__name__)

# Rough DeepSeek list prices, USD per 1M tokens. Used only to report a cost
# estimate in the digest; wrong prices produce a wrong number, never a failure.
PRICE_INPUT_PER_MTOK = 0.27
PRICE_OUTPUT_PER_MTOK = 1.10

FEATURED_LOOKBACK_DAYS = 7


def _feed_signature(title: str, summary: str) -> tuple[str, str]:
    """(exact-dup hash, simhash hex) for the text we actually compare on."""
    combined = f"{title} {summary}".strip()
    return content_hash(combined), to_hex(simhash(combined))


def heuristic_score(article: Article) -> float:
    """Free pre-model ranking signal; see llm/fake.py for the weights."""
    return heuristic_enrich(
        article.canonical_url, article.title, article.summary, article.source_weight
    ).score


async def ingest(settings, conn, fetcher: Fetcher) -> dict[str, int]:
    """Fetch every configured feed with conditional GET, then persist new entries."""
    sources = load_sources(settings.sources_path)
    log.info("ingesting %d sources", len(sources))

    states = {source.name: db.get_feed_state(conn, source.name) for source in sources}
    results = await asyncio.gather(
        *(
            fetcher.fetch(
                source.url,
                etag=states[source.name]["etag"] if states[source.name] else None,
                last_modified=(
                    states[source.name]["last_modified"] if states[source.name] else None
                ),
            )
            for source in sources
        )
    )

    stats = {
        "sources": len(sources),
        "fetched_ok": 0,
        "not_modified": 0,
        "fetch_failed": 0,
        "entries": 0,
        "new_articles": 0,
        "updated_articles": 0,
    }

    for source, result in zip(sources, results):
        if result.error:
            stats["fetch_failed"] += 1
            log.warning("fetch failed: %s (%s)", source.name, result.error)
            db.save_feed_state(conn, name=source.name, url=source.url, error=result.error)
            continue

        if result.not_modified:
            stats["not_modified"] += 1
            db.save_feed_state(
                conn,
                name=source.name,
                url=source.url,
                etag=result.etag,
                last_modified=result.last_modified,
            )
            continue

        raw_articles, error = parse_feed_lenient(
            result.content or b"", source, settings.per_source_limit
        )
        db.save_feed_state(
            conn,
            name=source.name,
            url=source.url,
            etag=result.etag,
            last_modified=result.last_modified,
            error=error,
        )
        if error:
            stats["fetch_failed"] += 1
            log.warning("parse failed: %s (%s)", source.name, error)
            continue

        stats["fetched_ok"] += 1
        articles: list[Article] = []
        for raw in raw_articles:
            exact_hash, sim_hex = _feed_signature(raw.title, raw.summary)
            articles.append(
                Article(
                    source=raw.source,
                    url=raw.url,
                    title=raw.title,
                    summary=raw.summary,
                    published_at=raw.published_at,
                    source_weight=raw.source_weight,
                    tags=raw.tags,
                    canonical_url=canonicalize_url(raw.url),
                    content_hash=exact_hash,
                    simhash_hex=sim_hex,
                )
            )

        inserted, updated = db.save_articles(conn, articles)
        stats["entries"] += len(articles)
        stats["new_articles"] += inserted
        stats["updated_articles"] += updated
        log.info("%s: %d entries (%d new)", source.name, len(articles), inserted)

    return stats


def select_clusters(conn, settings) -> tuple[list[Cluster], dict[str, int]]:
    """Load the candidate window, drop stories we already shipped, then cluster."""
    since = utcnow() - timedelta(days=settings.max_age_days)
    candidates = db.load_recent_articles(conn, since, limit=400)
    featured = db.recently_featured_urls(conn, days=FEATURED_LOOKBACK_DAYS)
    fresh = [a for a in candidates if a.canonical_url not in featured]
    clusters = cluster_articles(fresh, threshold=settings.near_dup_threshold)

    return clusters, {
        "candidates": len(candidates),
        "already_featured": len(candidates) - len(fresh),
        "after_dedupe": len(clusters),
        "near_duplicates": len(fresh) - len(clusters),
    }


def build_digest(
    settings,
    *,
    clusters: list[Cluster],
    scores,
    digest_date: date,
    stats: dict,
) -> Digest:
    rows: list[DigestItem] = []
    for cluster in clusters:
        rep = cluster.representative
        enriched = scores.get(rep.canonical_url)
        rows.append(
            DigestItem(
                title=rep.title,
                url=rep.url,
                source=rep.source,
                published_at=rep.published_at,
                one_liner=enriched.one_liner if enriched else rep.summary[:180],
                bullets=enriched.bullets if enriched else [],
                score=enriched.score if enriched else 50.0,
                topic=enriched.topic if enriched else "other",
                also_reported_by=[s for s in cluster.sources if s != rep.source],
            )
        )

    rows.sort(key=lambda item: item.score, reverse=True)

    # Diversity constraint. Ranking by score alone lets one prolific feed own
    # the whole page -- on the first real run one blog's release notes took 6 of
    # 12 slots. Cap per source, then backfill from the overflow if the cap left
    # the digest short.
    per_source: Counter[str] = Counter()
    selected: list[DigestItem] = []
    overflow: list[DigestItem] = []
    for item in rows:
        if len(selected) >= settings.top_n:
            overflow.append(item)
            continue
        if per_source[item.source] < settings.max_per_source:
            per_source[item.source] += 1
            selected.append(item)
        else:
            overflow.append(item)

    for item in overflow:
        if len(selected) >= settings.top_n:
            break
        selected.append(item)

    selected.sort(key=lambda item: item.score, reverse=True)
    for index, item in enumerate(selected, start=1):
        item.rank = index

    return Digest(digest_date=digest_date, items=selected, stats=dict(stats))


async def run_digest(
    settings,
    *,
    llm: LLMClient | None = None,
    fetcher: Fetcher | None = None,
) -> Digest:
    """Run the whole pipeline and persist the result. Raises on hard failure."""
    llm = llm or build_llm(settings)
    fetcher = fetcher or Fetcher(
        timeout=settings.fetch_timeout,
        retries=settings.max_retries,
        concurrency=settings.fetch_concurrency,
        user_agent=settings.user_agent,
    )
    digest_date: date = datetime.now().astimezone().date()
    started = utcnow()

    with db.open_db(settings.database_path) as conn:
        run_id = db.start_run(conn)
        stats: dict = {}
        try:
            stats.update(await ingest(settings, conn, fetcher))

            clusters, selection_stats = select_clusters(conn, settings)
            stats.update(selection_stats)

            # Two-stage ranking. A 100-item day would otherwise pay the model to
            # summarise 100 stories and publish 12. The heuristic is free and
            # good enough to pick the shortlist; the model does the final order.
            shortlist = sorted(
                clusters,
                key=lambda c: heuristic_score(c.representative),
                reverse=True,
            )[: settings.prefilter_n]
            stats["prefiltered"] = len(shortlist)

            scores, llm_stats = await enrich_articles(
                [c.representative for c in shortlist],
                llm,
                batch_size=settings.llm_batch_size,
                concurrency=settings.llm_concurrency,
            )
            stats.update(llm_stats)
            stats["enriched"] = len(scores)

            digest = build_digest(
                settings,
                clusters=shortlist,
                scores=scores,
                digest_date=digest_date,
                stats=stats,
            )

            usage = getattr(llm, "usage", {}) or {}
            cost = (
                usage.get("prompt_tokens", 0) / 1_000_000 * PRICE_INPUT_PER_MTOK
                + usage.get("completion_tokens", 0) / 1_000_000 * PRICE_OUTPUT_PER_MTOK
            )
            digest.stats.update(
                {
                    "llm_provider": getattr(llm, "name", "unknown"),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "estimated_cost_usd": round(cost, 6),
                    "duration_seconds": round((utcnow() - started).total_seconds(), 1),
                }
            )

            db.save_digest(conn, digest)
            db.finish_run(conn, run_id, "ok", digest.stats)
            return digest
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            stats["error"] = f"{type(exc).__name__}: {exc}"
            db.finish_run(conn, run_id, "error", stats)
            raise
