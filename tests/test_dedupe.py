from __future__ import annotations

from datetime import datetime, timezone

from ai_digest.models import Article
from ai_digest.process.dedupe import cluster_articles
from ai_digest.process.normalize import canonicalize_url, content_hash
from ai_digest.process.simhash import simhash, to_hex

TITLE = "OpenAI releases a faster inference stack"
SUMMARY = (
    "A new kernel cuts latency by 40 percent on H100 GPUs, and the library is "
    "open source under an Apache license."
)


def make_article(url: str, source: str, *, title: str = TITLE, summary: str = SUMMARY,
                 weight: float = 1.0) -> Article:
    text = f"{title} {summary}".strip()
    return Article(
        source=source,
        url=url,
        title=title,
        summary=summary,
        published_at=datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc),
        source_weight=weight,
        canonical_url=canonicalize_url(url),
        content_hash=content_hash(text),
        simhash_hex=to_hex(simhash(text)),
    )


def test_same_story_from_two_sources_is_one_cluster() -> None:
    articles = [
        make_article("https://a.example/1", "Feed A", weight=1.2),
        make_article("https://b.example/1", "Feed B", weight=1.0),
    ]
    clusters = cluster_articles(articles, threshold=3)

    assert len(clusters) == 1
    assert clusters[0].duplicate_count == 1
    assert clusters[0].sources == ["Feed A", "Feed B"]


def test_representative_prefers_higher_weight_source() -> None:
    articles = [
        make_article("https://b.example/1", "Feed B", weight=0.8),
        make_article("https://a.example/1", "Feed A", weight=1.3),
    ]
    clusters = cluster_articles(articles, threshold=3)
    assert clusters[0].representative.source == "Feed A"


def test_distinct_stories_stay_separate() -> None:
    articles = [
        make_article("https://a.example/1", "Feed A"),
        make_article(
            "https://b.example/2",
            "Feed B",
            title="Scaling laws for multimodal pretraining",
            summary=(
                "Compute allocation should shift as the number of modalities grows, "
                "according to a new paper from a Berkeley group."
            ),
        ),
    ]
    assert len(cluster_articles(articles, threshold=3)) == 2


def test_threshold_controls_sensitivity() -> None:
    a = make_article("https://a.example/1", "Feed A")
    b = make_article(
        "https://b.example/1",
        "Feed B",
        summary=(
            "A new kernel cuts latency by 40 percent on H100 GPUs, and the library "
            "ships open source under an Apache license."
        ),
    )
    # Very permissive threshold merges reworded coverage; threshold 0 only merges
    # byte-identical text.
    assert len(cluster_articles([a, b], threshold=64)) == 1
    assert len(cluster_articles([a, b], threshold=0)) == 2


def test_empty_input() -> None:
    assert cluster_articles([]) == []
