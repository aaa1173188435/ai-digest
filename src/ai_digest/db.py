"""SQLite persistence.

Deliberately plain sqlite3 rather than an ORM: the schema is five tables, and
hand-written SQL is easier to review than a stack of model definitions. Every
write is idempotent, which is what lets the pipeline re-run after a partial
failure without producing duplicate stories.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from .models import Article, Digest
from .process.normalize import canonicalize_url


def iso_utc(value: datetime | None) -> str | None:
    """Normalise to UTC ISO-8601 so string comparison in SQL is meaningful.

    Feed timestamps arrive with whatever offset the publisher used. Storing
    ``2026-11-18T06:00:00+02:00`` next to ``2026-11-18T04:30:00+00:00`` and
    comparing them as text silently gives the wrong ORDER BY.
    """
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url TEXT NOT NULL UNIQUE,
    url           TEXT NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    published_at  TEXT,
    content_hash  TEXT NOT NULL,
    simhash_hex   TEXT NOT NULL,
    source_weight REAL NOT NULL DEFAULT 1.0,
    tags          TEXT NOT NULL DEFAULT '[]',
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);

CREATE TABLE IF NOT EXISTS feeds (
    name            TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    etag            TEXT,
    last_modified   TEXT,
    last_fetched_at TEXT,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    digest_date  TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stats       TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# -- articles -------------------------------------------------------------


def save_articles(conn: sqlite3.Connection, articles: list[Article]) -> tuple[int, int]:
    """Insert new articles, refresh existing ones. Returns (inserted, skipped)."""
    if not articles:
        return 0, 0

    urls = [a.canonical_url for a in articles]
    placeholders = ",".join("?" * len(urls))
    existing = {
        row["canonical_url"]
        for row in conn.execute(
            f"SELECT canonical_url FROM articles WHERE canonical_url IN ({placeholders})",
            urls,
        )
    }

    rows = [
        (
            a.canonical_url,
            a.url,
            a.source,
            a.title,
            a.summary,
            iso_utc(a.published_at),
            a.content_hash,
            a.simhash_hex,
            a.source_weight,
            json.dumps(a.tags, ensure_ascii=False),
            iso_utc(a.fetched_at) or iso_utc(datetime.now()),
        )
        for a in articles
    ]
    conn.executemany(
        """
        INSERT INTO articles
            (canonical_url, url, source, title, summary, published_at,
             content_hash, simhash_hex, source_weight, tags, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_url) DO UPDATE SET
            -- The same URL reaches us from several feeds (an aggregator and the
            -- publisher both link to the article). The more authoritative feed
            -- must win, or a community aggregator's bookkeeping overwrites the
            -- publisher's own title and summary.
            source        = CASE WHEN excluded.source_weight > articles.source_weight
                                 THEN excluded.source ELSE articles.source END,
            title         = CASE WHEN excluded.source_weight > articles.source_weight
                                 THEN excluded.title ELSE articles.title END,
            summary       = CASE WHEN excluded.source_weight > articles.source_weight
                                 THEN excluded.summary ELSE articles.summary END,
            tags          = CASE WHEN excluded.source_weight > articles.source_weight
                                 THEN excluded.tags ELSE articles.tags END,
            source_weight = MAX(excluded.source_weight, articles.source_weight),
            published_at  = COALESCE(articles.published_at, excluded.published_at),
            fetched_at    = excluded.fetched_at
        """,
        rows,
    )
    inserted = sum(1 for u in urls if u not in existing)
    return inserted, len(urls) - inserted


def load_recent_articles(
    conn: sqlite3.Connection, since: datetime, limit: int = 500
) -> list[Article]:
    """Articles published (or, failing that, first seen) inside the window.

    Filtering on ``fetched_at`` alone would drag a decade-old blog archive into
    today's digest the first time a feed is added, so publication time wins when
    the feed provides it.
    """
    rows = conn.execute(
        """
        SELECT * FROM articles
        WHERE COALESCE(published_at, fetched_at) >= ?
        ORDER BY COALESCE(published_at, fetched_at) DESC
        LIMIT ?
        """,
        (iso_utc(since), limit),
    ).fetchall()
    return [
        Article(
            id=row["id"],
            canonical_url=row["canonical_url"],
            url=row["url"],
            source=row["source"],
            title=row["title"],
            summary=row["summary"],
            published_at=datetime.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            content_hash=row["content_hash"],
            simhash_hex=row["simhash_hex"],
            source_weight=row["source_weight"],
            tags=json.loads(row["tags"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
        )
        for row in rows
    ]


# -- feed state (conditional GET) -----------------------------------------


def get_feed_state(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE name = ?", (name,)).fetchone()


def save_feed_state(
    conn: sqlite3.Connection,
    *,
    name: str,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO feeds (name, url, etag, last_modified, last_fetched_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            url             = excluded.url,
            etag            = COALESCE(excluded.etag, feeds.etag),
            last_modified   = COALESCE(excluded.last_modified, feeds.last_modified),
            last_fetched_at = excluded.last_fetched_at,
            last_error      = excluded.last_error
        """,
        (
            name,
            url,
            etag,
            last_modified,
            datetime.now().astimezone().isoformat(),
            error,
        ),
    )


# -- digests --------------------------------------------------------------


def save_digest(conn: sqlite3.Connection, digest: Digest) -> None:
    conn.execute(
        """
        INSERT INTO digests (digest_date, generated_at, payload)
        VALUES (?, ?, ?)
        ON CONFLICT(digest_date) DO UPDATE SET
            generated_at = excluded.generated_at,
            payload      = excluded.payload
        """,
        (
            digest.digest_date.isoformat(),
            digest.generated_at.isoformat(),
            digest.model_dump_json(),
        ),
    )


def list_digest_dates(conn: sqlite3.Connection, limit: int = 60) -> list[str]:
    rows = conn.execute(
        "SELECT digest_date FROM digests ORDER BY digest_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [row["digest_date"] for row in rows]


def list_digests(conn: sqlite3.Connection, limit: int = 400) -> list[Digest]:
    """Every stored digest, newest first. Used to rebuild the whole static site."""
    rows = conn.execute(
        "SELECT payload FROM digests ORDER BY digest_date DESC LIMIT ?", (limit,)
    ).fetchall()

    digests: list[Digest] = []
    for row in rows:
        try:
            digests.append(Digest.model_validate_json(row["payload"]))
        except ValidationError:
            continue  # a corrupt row must not break the whole site build
    return digests


def recently_featured_urls(conn: sqlite3.Connection, days: int = 7) -> set[str]:
    """URLs already shipped in a digest during the last ``days`` days.

    Without this the same three stories would lead the digest every morning:
    "recent" is a rolling window, so a good story stays eligible indefinitely
    unless we remember that it has already had its turn.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT payload FROM digests WHERE digest_date >= ?", (since,)
    ).fetchall()

    urls: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        for item in payload.get("items") or []:
            url = item.get("url")
            if url:
                urls.add(canonicalize_url(url))
    return urls


# -- run log (observability) ----------------------------------------------


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
        (datetime.now().astimezone().isoformat(),),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, stats = ? WHERE id = ?",
        (
            datetime.now().astimezone().isoformat(),
            status,
            json.dumps(stats, ensure_ascii=False, default=str),
            run_id,
        ),
    )
