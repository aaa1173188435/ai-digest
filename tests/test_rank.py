from __future__ import annotations

from datetime import datetime, timezone

from ai_digest.models import Article
from ai_digest.process.rank import (
    VERSION_RELEASE_SCORE_CAP,
    _validate,
    is_version_release,
)


def make_article(title: str, url: str = "https://x.example/1") -> Article:
    return Article(
        source="Feed A",
        url=url,
        title=title,
        summary="summary text",
        published_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
        canonical_url=url,
        content_hash="hash-" + url,
        simhash_hex="0" * 16,
    )


class TestIsVersionRelease:
    def test_matches_package_with_version(self) -> None:
        assert is_version_release("llm 0.37.3")
        assert is_version_release("datasette-maplibre 0.1a0")
        assert is_version_release("datasette 1.0a43")
        assert is_version_release("jQuery 4.1.0")
        assert is_version_release("vLLM 0.9.0")
        assert is_version_release("  solid 2.0  ")

    def test_does_not_match_real_headlines(self) -> None:
        assert not is_version_release("Introducing GPT-6.2 Astra and Sol")
        assert not is_version_release("Understanding Positional Embeddings in Modern LLMs")
        assert not is_version_release("External sandboxes for coding agents")
        # A version number inside a sentence is still a headline.
        assert not is_version_release("vLLM 0.9.0 ships a new attention kernel")
        assert not is_version_release("")


class TestVersionCapIsApplied:
    def test_high_model_score_is_clamped(self) -> None:
        article = make_article("llm 0.37.3")
        data = {
            "items": [
                {
                    "id": article.canonical_url,
                    "one_liner": "一个小版本发布",
                    "bullets": [],
                    "score": 58,
                    "topic": "tools",
                }
            ]
        }
        scores, missing = _validate([article], data)

        assert not missing
        assert scores[article.canonical_url].score == VERSION_RELEASE_SCORE_CAP

    def test_technical_headline_is_untouched(self) -> None:
        article = make_article("Understanding Positional Embeddings in Modern LLMs")
        data = {
            "items": [
                {
                    "id": article.canonical_url,
                    "one_liner": "位置嵌入综述",
                    "bullets": [],
                    "score": 78,
                    "topic": "research",
                }
            ]
        }
        scores, _ = _validate([article], data)

        assert scores[article.canonical_url].score == 78

    def test_already_low_score_is_not_raised(self) -> None:
        article = make_article("datasette 0.65.6")
        data = {
            "items": [
                {
                    "id": article.canonical_url,
                    "one_liner": "x",
                    "score": 5,
                    "topic": "tools",
                }
            ]
        }
        scores, _ = _validate([article], data)
        assert scores[article.canonical_url].score == 5
