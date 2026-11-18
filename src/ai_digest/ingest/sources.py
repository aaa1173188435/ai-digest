"""Feed configuration.

Sources live in YAML rather than code so that adding a feed is a config diff,
not a code change -- and so the GitHub Actions workflow can override the file
without touching Python.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..models import Source

DEFAULT_SOURCES: list[dict] = [
    {"name": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml", "weight": 1.3, "tags": ["research"]},
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 1.3, "tags": ["industry"]},
    {"name": "Google AI", "url": "https://blog.google/technology/ai/rss/", "weight": 1.2, "tags": ["research"]},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/", "weight": 1.2, "tags": ["llm", "tools"]},
    {"name": "Lilian Weng", "url": "https://lilianweng.github.io/index.xml", "weight": 1.1, "tags": ["research"]},
    {"name": "Sebastian Raschka", "url": "https://sebastianraschka.com/rss_feed.xml", "weight": 1.1, "tags": ["engineering"]},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "weight": 1.1, "tags": ["industry"]},
    {"name": "BAIR", "url": "https://bair.berkeley.edu/blog/feed.xml", "weight": 1.0, "tags": ["research"]},
    {"name": "The Gradient", "url": "https://thegradient.pub/rss/", "weight": 1.0, "tags": ["analysis"]},
    {"name": "Latent Space", "url": "https://www.latent.space/feed", "weight": 1.0, "tags": ["engineering"]},
    {"name": "HN 200+", "url": "https://hnrss.org/frontpage?points=200", "weight": 0.8, "tags": ["community"]},
]


def load_sources(path: Path) -> list[Source]:
    """Load enabled sources from YAML, falling back to the built-in list."""
    if not path.exists():
        return [Source(**item) for item in DEFAULT_SOURCES]

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("sources", []) if isinstance(data, dict) else data
    sources = [Source(**item) for item in raw]
    return [source for source in sources if source.enabled]
