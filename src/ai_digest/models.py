"""Domain models.

Pydantic models double as the validation boundary for every untrusted input:
feed XML, LLM JSON, and rows read back from SQLite. Anything that crosses a
process boundary in this codebase has a model here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(BaseModel):
    """A feed we poll, as declared in config/sources.yaml."""

    name: str
    url: str
    weight: float = 1.0
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class RawArticle(BaseModel):
    """One feed entry, straight out of the parser, before cleaning."""

    source: str
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    source_weight: float = 1.0
    tags: list[str] = Field(default_factory=list)


class Article(RawArticle):
    """A canonicalised article as persisted in the database."""

    id: int | None = None
    canonical_url: str
    content_hash: str
    simhash_hex: str
    fetched_at: datetime = Field(default_factory=utcnow)


class ItemScore(BaseModel):
    """LLM-produced enrichment for one article. Validated, never trusted raw."""

    id: str
    one_liner: str = ""
    bullets: list[str] = Field(default_factory=list)
    score: float = 50.0
    topic: str = "other"


class DigestItem(BaseModel):
    rank: int = 0
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    one_liner: str = ""
    bullets: list[str] = Field(default_factory=list)
    score: float = 50.0
    topic: str = "other"
    also_reported_by: list[str] = Field(default_factory=list)


class Digest(BaseModel):
    digest_date: date
    generated_at: datetime = Field(default_factory=utcnow)
    items: list[DigestItem] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
