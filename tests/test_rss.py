from __future__ import annotations

import pytest

from ai_digest.ingest.rss import parse_feed, parse_feed_lenient, strip_html
from ai_digest.models import Source

SOURCE = Source(name="Feed A", url="https://a.example/rss", weight=1.2)


@pytest.mark.parametrize("limit", [1])
def test_limit_is_respected(feed_a: bytes, limit: int) -> None:
    assert len(parse_feed(feed_a, SOURCE, limit=limit)) == limit


def test_parses_entries(feed_a: bytes) -> None:
    articles = parse_feed(feed_a, SOURCE, limit=10)
    assert len(articles) == 2
    assert articles[0].title == "OpenAI releases a faster inference stack"
    assert articles[0].source == "Feed A"
    assert articles[0].source_weight == 1.2


def test_strips_html_from_summary(feed_a: bytes) -> None:
    articles = parse_feed(feed_a, SOURCE, limit=10)
    assert "<p>" not in articles[0].summary
    assert "cuts latency by 40%" in articles[0].summary


def test_parses_published_date_as_utc(feed_a: bytes) -> None:
    published = parse_feed(feed_a, SOURCE, limit=10)[0].published_at
    assert published is not None
    assert published.tzinfo is not None
    assert published.year == 2026


def test_malformed_feed_raises() -> None:
    with pytest.raises(ValueError):
        parse_feed(b"this is definitely not xml", SOURCE)


def test_lenient_variant_returns_error_instead_of_raising() -> None:
    articles, error = parse_feed_lenient(b"<not-a-feed>", SOURCE)
    assert articles == []
    assert error is not None


def test_strip_html_truncates_on_word_boundary() -> None:
    stripped = strip_html("<p>" + "word " * 400 + "</p>", limit=100)
    assert len(stripped) <= 104
    assert stripped.endswith("...")


def test_strips_markup_from_title() -> None:
    # Seen from a real feed: "<br>" survives into the rendered page otherwise.
    feed = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>T</title>
    <item>
      <title>Intelligence is Free<br> Now What? A &amp; B</title>
      <link>https://x.example/1</link>
      <description>body</description>
    </item>
    </channel></rss>"""
    articles = parse_feed(feed, SOURCE, limit=5)
    assert articles[0].title == "Intelligence is Free Now What? A & B"


def test_strips_aggregator_metadata_from_summary() -> None:
    # Hacker News puts its own bookkeeping in <description>; feeding it to the
    # model produced "the HN discussion is very active" as a summary.
    feed = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>T</title>
    <item>
      <title>Some launch</title>
      <link>https://x.example/2</link>
      <description>Article URL: https://x.example/2 Comments URL: https://news.ycombinator.com/item?id=1 Points: 1138 # Comments: 720</description>
    </item>
    </channel></rss>"""
    articles = parse_feed(feed, SOURCE, limit=5)
    assert articles[0].summary == ""
