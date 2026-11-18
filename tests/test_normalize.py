from __future__ import annotations

from ai_digest.process.normalize import canonicalize_url, content_hash, normalize_text


class TestCanonicalizeUrl:
    def test_strips_tracking_and_fragment(self) -> None:
        url = "https://Example.COM/Post/?utm_source=x&utm_medium=y&fbclid=z#section"
        assert canonicalize_url(url) == "https://example.com/Post"

    def test_keeps_meaningful_query_sorted(self) -> None:
        url = "https://example.com/archive?page=2&tag=ai"
        assert canonicalize_url(url) == "https://example.com/archive?page=2&tag=ai"

    def test_drops_www_and_default_port(self) -> None:
        assert canonicalize_url("https://www.example.com:443/a") == "https://example.com/a"

    def test_keeps_non_default_port(self) -> None:
        assert canonicalize_url("http://example.com:8080/a") == "http://example.com:8080/a"

    def test_does_not_force_https(self) -> None:
        # http and https are kept distinct: on some hosts they serve different pages.
        assert canonicalize_url("http://example.com/a") != canonicalize_url(
            "https://example.com/a"
        )

    def test_non_http_passthrough(self) -> None:
        assert canonicalize_url("mailto:hi@example.com") == "mailto:hi@example.com"

    def test_trailing_slash_collapses(self) -> None:
        assert canonicalize_url("https://example.com/a/") == canonicalize_url(
            "https://example.com/a"
        )


class TestNormalizeText:
    def test_collapses_whitespace_and_nbsp(self) -> None:
        assert normalize_text("hello\u00a0   world\n\t!") == "hello world !"

    def test_strips_zero_width(self) -> None:
        assert normalize_text("a\u200bb") == "a b"


class TestContentHash:
    def test_ignores_whitespace_and_case(self) -> None:
        assert content_hash("Hello   World") == content_hash("hello world")

    def test_differs_on_content(self) -> None:
        assert content_hash("hello") != content_hash("hello!")
