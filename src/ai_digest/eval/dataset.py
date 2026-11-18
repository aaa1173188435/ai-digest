"""Eval data model: a frozen candidate pool plus human labels.

The point of freezing a case is reproducibility. Tomorrow's feeds are different,
so an eval that re-fetches can never tell you whether today's prompt change
helped. ``snapshot`` writes the exact texts the ranker saw; every later run
replays those texts.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..models import utcnow
from ..process.dedupe import cluster_articles


class EvalItem(BaseModel):
    """One candidate, as the ranker sees it. No scores: those are the output."""

    id: str  # canonical_url, stable across runs
    title: str
    source: str
    summary: str = ""


class EvalCase(BaseModel):
    case_id: str
    created_at: datetime = Field(default_factory=utcnow)
    items: list[EvalItem] = Field(default_factory=list)
    notes: str = ""

    def ids(self) -> list[str]:
        return [item.id for item in self.items]


class EvalLabels(BaseModel):
    case_id: str
    # id -> 1 (an AI engineer should read it) or 0. Absent id = unlabelled, and
    # unlabelled items are excluded from the metrics' denominator rather than
    # silently counted as irrelevant.
    labels: dict[str, int] = Field(default_factory=dict)
    labeled_at: datetime = Field(default_factory=utcnow)
    labeler: str = ""

    def coverage(self, case: EvalCase) -> float:
        if not case.items:
            return 0.0
        labelled = sum(1 for item in case.items if item.id in self.labels)
        return round(labelled / len(case.items), 3)


def save_json(model: BaseModel, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_case(path: Path) -> EvalCase:
    return EvalCase.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_labels(path: Path) -> EvalLabels:
    return EvalLabels.model_validate_json(Path(path).read_text(encoding="utf-8"))


def case_from_db(
    conn,
    *,
    case_id: str,
    max_age_days: int = 4,
    near_dup_threshold: int = 3,
    limit: int = 400,
) -> EvalCase:
    """Freeze the candidate pool that a run on this date would have considered.

    Deliberately ignores "already featured": the question we are grading is
    "which of these should be in today's digest", not "which are still
    available".
    """
    since = utcnow() - timedelta(days=max_age_days)
    articles = db.load_recent_articles(conn, since, limit=limit)
    clusters = cluster_articles(articles, threshold=near_dup_threshold)

    items = [
        EvalItem(
            id=cluster.representative.canonical_url,
            title=cluster.representative.title,
            source=cluster.representative.source,
            summary=cluster.representative.summary,
        )
        for cluster in clusters
    ]
    return EvalCase(
        case_id=case_id,
        items=items,
        notes=f"frozen from {len(articles)} articles -> {len(items)} clusters",
    )


def default_case_id(today: date | None = None) -> str:
    return (today or datetime.now().astimezone().date()).isoformat()


def write_partial_labels_stub(case: EvalCase, path: Path) -> Path:
    """An empty, correct-shaped label file for hand editing."""
    return save_json(
        EvalLabels(case_id=case.case_id, labels={}, labeler=""), path
    )


def save_report(report: dict, reports_dir: Path) -> Path:
    """Persist one eval run so later runs have something to compare against."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"{report['case_id']}-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
