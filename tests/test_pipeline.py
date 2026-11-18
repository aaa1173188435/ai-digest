from __future__ import annotations

from datetime import date

import pytest

from ai_digest import db
from ai_digest.llm.base import LLMError
from ai_digest.llm.fake import FakeLLM
from ai_digest.models import Article, ItemScore
from ai_digest.pipeline import build_digest, run_digest
from ai_digest.process.dedupe import Cluster
from ai_digest.render.html import rebuild_site
from ai_digest.render.markdown import render_markdown


async def test_end_to_end_builds_digest(settings, fetch) -> None:
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    # 4 feed entries, two of which are the same story.
    assert digest.stats["new_articles"] == 4
    assert digest.stats["near_duplicates"] == 1
    assert len(digest.items) == 3

    assert all(item.one_liner for item in digest.items)
    assert all(0 <= item.score <= 100 for item in digest.items)
    assert [i.rank for i in digest.items] == [1, 2, 3]


async def test_duplicate_story_reports_both_sources(settings, fetch) -> None:
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    merged = next(i for i in digest.items if i.title.startswith("OpenAI releases"))
    assert merged.source == "Feed A"  # higher weight wins
    assert "Feed B" in merged.also_reported_by


async def test_items_are_sorted_by_score(settings, fetch) -> None:
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)
    scores = [item.score for item in digest.items]
    assert scores == sorted(scores, reverse=True)


async def test_ingest_is_idempotent(settings, fetch) -> None:
    await run_digest(settings, llm=FakeLLM(), fetcher=fetch)
    await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    with db.open_db(settings.database_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]

    assert count == 4  # re-running never duplicates rows


async def test_already_shipped_stories_are_not_repeated(settings, fetch) -> None:
    first = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)
    second = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    assert second.stats["already_featured"] == len(first.items)


async def test_llm_failure_degrades_to_heuristics(settings, fetch) -> None:
    class FailingLLM:
        name = "failing"
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

        async def complete_json(self, *, system: str, user: str) -> dict:
            raise LLMError("simulated provider outage")

    digest = await run_digest(settings, llm=FailingLLM(), fetcher=fetch)

    assert len(digest.items) == 3  # still shipped
    assert digest.stats["llm_failures"] == 1
    assert digest.stats["heuristic_items"] == 3


async def test_failed_feed_does_not_kill_the_run(settings, fetch) -> None:
    # Ask for a URL the mock transport answers with 404.
    settings.sources_path.write_text(
        "sources:\n"
        "  - name: Feed A\n"
        "    url: https://a.example/rss\n"
        "  - name: Dead Feed\n"
        "    url: https://dead.example/rss\n",
        encoding="utf-8",
    )
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    assert digest.stats["fetch_failed"] == 1
    assert digest.stats["fetched_ok"] == 1
    assert len(digest.items) >= 1


async def test_write_site_produces_page(settings, fetch, tmp_path) -> None:
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)

    with db.open_db(settings.database_path) as conn:
        paths = rebuild_site(conn, settings.output_dir)

    index = settings.output_dir / "index.html"
    assert index in paths
    html = index.read_text(encoding="utf-8")
    assert digest.items[0].title in html
    assert f"{digest.digest_date.isoformat()}" in html
    assert (settings.output_dir / "archive.html").exists()
    assert (settings.output_dir / "digests" / f"{digest.digest_date}.html").exists()


async def test_render_markdown_contains_items(settings, fetch) -> None:
    digest = await run_digest(settings, llm=FakeLLM(), fetcher=fetch)
    markdown = render_markdown(digest)

    assert markdown.startswith("# AI Digest")
    assert digest.items[0].title in markdown
    assert "另见" in markdown  # the merged story mentions the second source


# -- ranking: diversity ----------------------------------------------------


def _fake_article(index: int, source: str) -> Article:
    url = f"https://{source.lower().replace(' ', '-')}.example/{index}"
    return Article(
        source=source,
        url=url,
        title=f"Title {index}",
        summary="Some summary text.",
        canonical_url=url,
        content_hash=f"hash-{url}",
        simhash_hex="0" * 16,
    )


def _clusters_with_scores(
    spec: dict[str, list[float]],
) -> tuple[list[Cluster], dict[str, ItemScore]]:
    clusters: list[Cluster] = []
    scores: dict[str, ItemScore] = {}
    index = 0
    for source, values in spec.items():
        for value in values:
            article = _fake_article(index, source)
            index += 1
            clusters.append(Cluster(articles=[article]))
            scores[article.canonical_url] = ItemScore(
                id=article.canonical_url, one_liner="line", score=value
            )
    return clusters, scores


def test_one_source_cannot_own_the_whole_page(settings) -> None:
    settings.top_n = 6
    settings.max_per_source = 2
    clusters, scores = _clusters_with_scores(
        {"Feed A": [90, 88, 86], "Feed B": [80, 78, 76], "Feed C": [70, 68, 66]}
    )

    digest = build_digest(
        settings, clusters=clusters, scores=scores, digest_date=date(2026, 11, 18), stats={}
    )

    counts = {}
    for item in digest.items:
        counts[item.source] = counts.get(item.source, 0) + 1
    assert counts == {"Feed A": 2, "Feed B": 2, "Feed C": 2}
    assert len(digest.items) == 6


def test_digest_backfills_when_cap_leaves_it_short(settings) -> None:
    settings.top_n = 7
    settings.max_per_source = 2
    clusters, scores = _clusters_with_scores(
        {"Feed A": [90, 88, 86], "Feed B": [80, 78, 76], "Feed C": [70, 68, 66]}
    )

    digest = build_digest(
        settings, clusters=clusters, scores=scores, digest_date=date(2026, 11, 18), stats={}
    )

    assert len(digest.items) == 7  # cap first, then relax to fill the page


def test_ranks_are_contiguous_after_selection(settings) -> None:
    settings.top_n = 4
    clusters, scores = _clusters_with_scores({"Feed A": [90, 80], "Feed B": [70, 60, 50]})

    digest = build_digest(
        settings, clusters=clusters, scores=scores, digest_date=date(2026, 11, 18), stats={}
    )

    assert [item.rank for item in digest.items] == [1, 2, 3, 4]
    assert digest.items[0].score >= digest.items[-1].score


# -- candidate window ------------------------------------------------------


def test_old_articles_are_excluded_by_publication_date(settings) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = _fake_article(1, "Feed A")
    fresh.published_at = now - timedelta(days=1)
    stale = _fake_article(2, "Feed B")
    stale.published_at = datetime(2023, 5, 1, tzinfo=timezone.utc)

    with db.open_db(settings.database_path) as conn:
        db.save_articles(conn, [fresh, stale])
        loaded = db.load_recent_articles(conn, now - timedelta(days=4))

    titles = {a.title for a in loaded}
    assert "Title 1" in titles
    assert "Title 2" not in titles  # a 2023 post must not appear today


def test_undated_articles_fall_back_to_fetch_time(settings) -> None:
    from datetime import datetime, timedelta, timezone

    undated = _fake_article(3, "Feed A")
    undated.published_at = None

    with db.open_db(settings.database_path) as conn:
        db.save_articles(conn, [undated])
        loaded = db.load_recent_articles(
            conn, datetime.now(timezone.utc) - timedelta(days=1)
        )

    assert [a.title for a in loaded] == ["Title 3"]
