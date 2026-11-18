"""RSS / Atom parsing.

feedparser is forgiving to the point of being dangerous: on malformed XML it
sets ``bozo`` but still returns whatever entries it could scrape. We therefore
treat "zero entries" as an error and surface it, instead of silently writing an
empty digest.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from ..models import RawArticle, Source
from ..process.normalize import canonicalize_url, normalize_text

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def strip_html(text: str, limit: int = 800) -> str:
    text = _SCRIPT_RE.sub(" ", text or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = normalize_text(text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def _entry_time(entry: dict) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(field)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                parsed = parsedate_to_datetime(raw)
                aware = (
                    parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                )
                return aware.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def parse_feed(content: bytes, source: Source, limit: int = 10) -> list[RawArticle]:
    """Turn raw feed bytes into cleaned articles. Raises on unparseable input."""
    parsed = feedparser.parse(content)

    if parsed.bozo and not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "unknown parse error")
        raise ValueError(f"unparseable feed: {reason}")

    articles: list[RawArticle] = []
    seen: set[str] = set()

    for entry in parsed.entries[: limit * 2]:
        url = (entry.get("link") or "").strip()
        # Titles are html-ish in the wild ("... <br> ...", "A &amp; B"), so they
        # get the same treatment as summaries.
        title = strip_html(entry.get("title", ""), limit=300)
        if not url or not title:
            continue

        canonical = canonicalize_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)

        summary = strip_html(
            entry.get("summary") or entry.get("description") or ""
        )
        articles.append(
            RawArticle(
                source=source.name,
                url=url,
                title=title,
                summary=summary,
                published_at=_entry_time(entry),
                source_weight=source.weight,
                tags=source.tags,
            )
        )
        if len(articles) >= limit:
            break

    return articles


def parse_feed_lenient(
    content: bytes, source: Source, limit: int = 10
) -> tuple[list[RawArticle], str | None]:
    """Same as :func:`parse_feed` but returns ``(articles, error)`` instead of raising."""
    try:
        return parse_feed(content, source, limit), None
    except ValueError as exc:
        return [], str(exc)
