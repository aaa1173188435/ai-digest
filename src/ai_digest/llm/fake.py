"""Offline provider and the shared heuristic fallback.

This exists for two reasons, and it is worth being explicit about both:

* **Tests.** The whole pipeline is testable with no API key, no network, and no
  flakiness. Determinism beats realism in a unit test.
* **Degradation.** If a real LLM call fails mid-run, the pipeline falls back to
  :func:`heuristic_enrich` for that batch. The digest still ships, slightly
  dumber, instead of not shipping. A daily job that fails hard at 06:00 is worse
  than one that is 80% as good.
"""

from __future__ import annotations

import json
import re

from ..models import ItemScore

# keyword -> bonus points, ordered roughly by how much I care
SIGNAL_WEIGHTS: dict[str, float] = {
    "open source": 8,
    "open-source": 8,
    "release": 6,
    "released": 6,
    "benchmark": 6,
    "sota": 7,
    "state of the art": 7,
    "agent": 6,
    "reasoning": 5,
    "fine-tun": 4,
    "inference": 4,
    "quantization": 4,
    "paper": 3,
    "dataset": 3,
    "scaling": 3,
    "evaluation": 3,
    "safety": 3,
    "multimodal": 4,
    "llm": 3,
    "diffusion": 3,
    "transformer": 2,
    "fine tun": 4,
    "cost": 3,
    "pricing": 4,
    "api": 2,
    "hiring": -6,
    "webinar": -8,
    "sponsored": -10,
    "podcast": -4,
    "newsletter": -3,
}

TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("agents", ("agent", "tool use", "tool-calling", "workflow", "autogpt")),
    ("models", ("model", "gpt", "claude", "llama", "gemini", "qwen", "deepseek", "mistral")),
    ("research", ("paper", "arxiv", "sota", "benchmark", "theorem", "dataset")),
    ("infra", ("gpu", "inference", "serving", "vllm", "quantization", "cuda", "kernel")),
    ("tools", ("library", "framework", "sdk", "cli", "ide", "editor")),
    ("industry", ("funding", "acquisition", "launch", "partnership", "revenue", "ipo")),
]

_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _sentences(text: str, limit: int = 3) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]
    return parts[:limit]


def guess_topic(text: str) -> str:
    lowered = (text or "").lower()
    for topic, keywords in TOPIC_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return topic
    return "other"


def heuristic_enrich(
    item_id: str, title: str, summary: str, source_weight: float = 1.0
) -> ItemScore:
    """Deterministic stand-in for the model: keyword signals, no cleverness."""
    text = f"{title} {summary}".lower()
    score = 40.0
    for keyword, bonus in SIGNAL_WEIGHTS.items():
        if keyword in text:
            score += bonus
    score += (source_weight - 1.0) * 10
    if summary:
        score += min(len(summary) / 200.0, 1.0) * 4

    sentences = _sentences(summary) or [title]
    bullets = [s for s in sentences[1:3] if s]

    return ItemScore(
        id=item_id,
        one_liner=(sentences[0] if sentences else title)[:180],
        bullets=bullets,
        score=round(_clamp(score), 1),
        topic=guess_topic(text),
    )


class FakeLLM:
    """Reads the batch payload out of the prompt and answers with heuristics."""

    name = "fake"
    usage: dict[str, int]

    def __init__(self) -> None:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.usage["calls"] += 1
        marker = "ARTICLES_JSON:"
        payload = user.split(marker, 1)[-1].strip() if marker in user else "[]"
        try:
            articles = json.loads(payload)
        except json.JSONDecodeError:
            articles = []

        results = []
        for article in articles:
            enriched = heuristic_enrich(
                item_id=str(article.get("id", "")),
                title=article.get("title", ""),
                summary=article.get("summary", ""),
                source_weight=float(article.get("source_weight", 1.0)),
            )
            results.append(enriched.model_dump())
        return {"items": results}
