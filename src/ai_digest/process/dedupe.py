"""Near-duplicate clustering.

Two stages, cheapest first:

1. **Exact.** ``canonical_url`` is UNIQUE in SQLite, so re-ingesting a feed never
   duplicates a row. Nothing to do here but rely on it.
2. **Near.** Within a batch, greedy single-link clustering on the 64-bit
   simhash. Two articles join the same cluster if their Hamming distance is at
   most ``threshold`` bits (3 is roughly "same story, different wording").

Complexity is O(n^2) over the batch, which is fine at n ~ a few hundred. At 10^5
documents the right answer is MinHash + an LSH band index; that is deliberately
out of scope, and saying so is part of the design.

A note on the data model: a cluster stores *only* its articles. The
"representative" is derived, not stored. An earlier version stored both, and
re-picking the representative silently dropped the old one from the cluster --
a real bug that cost an incident, so the invariant is now enforced by the type
shape rather than by remembering to call a method.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Article
from .simhash import from_hex, hamming


@dataclass
class Cluster:
    articles: list[Article] = field(default_factory=list)

    @property
    def representative(self) -> Article:
        """The best write-up: more authoritative source, then richer summary.

        ``max`` returns the first maximal element, so ties keep ingestion order.
        """
        return max(self.articles, key=lambda a: (a.source_weight, len(a.summary)))

    @property
    def members(self) -> list[Article]:
        """Everything except the representative, in ingestion order."""
        rep = self.representative
        return [a for a in self.articles if a is not rep]

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for article in self.articles:
            if article.source not in seen:
                seen.append(article.source)
        return seen

    @property
    def duplicate_count(self) -> int:
        """How many *other* outlets ran the same story."""
        return max(0, len(self.articles) - 1)


def _similar(a: Article, b: Article, threshold: int) -> bool:
    if a.content_hash == b.content_hash:
        return True
    return hamming(from_hex(a.simhash_hex), from_hex(b.simhash_hex)) <= threshold


def cluster_articles(articles: list[Article], threshold: int = 3) -> list[Cluster]:
    """Greedy single-link clustering; new clusters are created in input order."""
    clusters: list[Cluster] = []

    for article in articles:
        for cluster in clusters:
            # Compare against every member, not just the representative: a chain
            # of slightly-different rewrites still collapses into one story.
            if any(_similar(article, other, threshold) for other in cluster.articles):
                cluster.articles.append(article)
                break
        else:
            clusters.append(Cluster(articles=[article]))

    return clusters


def dedupe_stats(articles: list[Article], clusters: list[Cluster]) -> dict[str, int]:
    return {
        "articles": len(articles),
        "clusters": len(clusters),
        "near_duplicates": len(articles) - len(clusters),
    }
