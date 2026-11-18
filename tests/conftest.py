from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from ai_digest.ingest.fetcher import Fetcher
from ai_digest.settings import Settings

FEED_A = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Feed A</title>
    <item>
      <title>OpenAI releases a faster inference stack</title>
      <link>https://a.example/post/1?utm_source=rss&amp;utm_medium=feed</link>
      <description>&lt;p&gt;A new kernel cuts latency by 40% on H100.&lt;/p&gt;</description>
      <pubDate>18 Nov 2026 06:00:00 GMT</pubDate>
    </item>
    <item>
      <title>We are hiring a research engineer</title>
      <link>https://a.example/post/2</link>
      <description>Join the team in San Francisco.</description>
      <pubDate>18 Nov 2026 05:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# Same story as A/post/1: different URL, different tracking params, same text.
FEED_B = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Feed B</title>
    <item>
      <title>OpenAI releases a faster inference stack</title>
      <link>https://www.b.example/news/1/?fbclid=abc123</link>
      <description>A new kernel cuts latency by 40% on H100.</description>
      <pubDate>18 Nov 2026 06:30:00 GMT</pubDate>
    </item>
    <item>
      <title>Scaling laws for multimodal pretraining</title>
      <link>https://b.example/news/2</link>
      <description>We study how compute allocation changes with modalities.</description>
      <pubDate>18 Nov 2026 04:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def feed_a() -> bytes:
    return FEED_A


@pytest.fixture
def feed_b() -> bytes:
    return FEED_B


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    sources = {
        "sources": [
            {"name": "Feed A", "url": "https://a.example/rss", "weight": 1.2},
            {"name": "Feed B", "url": "https://b.example/rss", "weight": 1.0},
        ]
    }
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(yaml.safe_dump(sources), encoding="utf-8")

    return Settings(
        llm_provider="fake",
        database_path=tmp_path / "data" / "test.db",
        sources_path=sources_path,
        output_dir=tmp_path / "public",
        max_retries=1,
        fetch_timeout=5.0,
    )


@pytest.fixture
def fetch(settings: Settings) -> Fetcher:
    responses = {
        "https://a.example/rss": (200, FEED_A),
        "https://b.example/rss": (200, FEED_B),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses.get(str(request.url), (404, b""))
        return httpx.Response(status, content=body, headers={"content-type": "application/xml"})

    return Fetcher(transport=httpx.MockTransport(handler), retries=1, timeout=5.0)
