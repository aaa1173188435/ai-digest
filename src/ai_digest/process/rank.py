"""LLM enrichment: one-liner, bullets, relevance score, topic.

Batching is the main cost lever. One request per 8 articles instead of one per
article amortises the ~300-token system prompt over 8 items, which is the
difference between a digest that costs a cent and one that costs a dime.

Everything the model returns is validated. Missing ids, wrong types, out-of-range
scores, and outright garbage all degrade to the heuristic rather than raising.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import ValidationError

from ..llm.base import LLMClient, LLMError
from ..llm.fake import heuristic_enrich
from ..models import Article, ItemScore

SYSTEM_PROMPT = (
    "你是一名资深 AI 领域技术编辑，为工程师读者编写每日简报。"
    "只输出 JSON，不要输出解释文字，不要用 markdown 代码块包裹。\n"
    "对每篇文章给出四个字段：\n"
    "- one_liner：一句话说明它讲什么、为什么重要，不超过 60 个汉字；\n"
    "- bullets：2-3 条要点，每条不超过 40 个汉字，尽量包含具体技术细节或数字；\n"
    "- score：0-100 的整数，表示对一名 AI 工程师的重要程度；\n"
    "- topic：只能从 models, agents, research, infra, tools, industry, other 中选一个。\n"
    "\n"
    "打分标准：核心问题是「读完之后，一名 AI 工程师能否获得可迁移的技术认知」。\n"
    "新模型、新方法、新工具、重要的失败教训 → 70 分以上。\n"
    "有信息量的技术分析或工程经验 → 50-70 分。\n"
    "\n"
    "以下情况一律给 30 分以下，无论措辞多么宏大：\n"
    "- 产品功能更新、促销、区域上线、企业签约、客户案例（不是技术发布）；\n"
    "- 纯版本号发布说明（如 x.y.z 的更新日志），除非它引入了新模型或新算法；\n"
    "- 与 AI 无关的内容：前端/数据库工具、安全公告、基础设施故障、硬件财报；\n"
    "- 政策、社会责任、公益项目、招聘、活动、广告。\n"
    "\n"
    "严禁编造原文中没有的事实、数字或结论。"
)

ALLOWED_TOPICS = {
    "models",
    "agents",
    "research",
    "infra",
    "tools",
    "industry",
    "other",
}


def build_user_prompt(articles: list[Article]) -> str:
    payload = [
        {
            "id": article.canonical_url,
            "source": article.source,
            "title": article.title,
            "summary": article.summary[:600],
            "source_weight": article.source_weight,
        }
        for article in articles
    ]

    return (
        f"处理以下 {len(articles)} 篇文章，返回 {{\"items\": [...]}}，"
        "每一项都必须带上对应的 id。\n\n"
        "ARTICLES_JSON:\n" + json.dumps(payload, ensure_ascii=False)
    )


def _coerce_score(raw: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 50.0
    return max(0.0, min(100.0, value))


def _validate(
    batch: list[Article], data: dict
) -> tuple[dict[str, ItemScore], list[str]]:
    """Map model output onto validated scores; report ids the model dropped."""
    wanted = {article.canonical_url for article in batch}
    by_id: dict[str, Article] = {article.canonical_url: article for article in batch}
    scores: dict[str, ItemScore] = {}
    raw_items = data.get("items")

    if not isinstance(raw_items, list):
        return {}, sorted(wanted)

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id", "")).strip()
        if item_id not in wanted or item_id in scores:
            continue
        try:
            score = ItemScore(
                id=item_id,
                one_liner=str(raw.get("one_liner") or "").strip()[:300],
                bullets=[
                    str(b).strip()[:200]
                    for b in (raw.get("bullets") or [])
                    if str(b).strip()
                ][:4],
                score=_coerce_score(raw.get("score", 50)),
                topic=str(raw.get("topic") or "other").strip().lower(),
            )
        except ValidationError:
            continue

        if score.topic not in ALLOWED_TOPICS:
            score.topic = "other"
        if not score.one_liner:
            score.one_liner = by_id[item_id].title[:180]
        scores[item_id] = score

    return scores, sorted(wanted - set(scores))


async def enrich_articles(
    articles: list[Article],
    llm: LLMClient,
    *,
    batch_size: int = 8,
    concurrency: int = 3,
) -> tuple[dict[str, ItemScore], dict[str, int]]:
    """Enrich articles in batches. Never raises: failures degrade to heuristics."""
    stats = {"llm_calls": 0, "llm_failures": 0, "heuristic_items": 0}
    if not articles:
        return {}, stats

    batches = [
        articles[i : i + batch_size] for i in range(0, len(articles), batch_size)
    ]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, ItemScore] = {}

    async def run_batch(batch: list[Article]) -> None:
        async with semaphore:
            try:
                data = await llm.complete_json(
                    system=SYSTEM_PROMPT, user=build_user_prompt(batch)
                )
                stats["llm_calls"] += 1
                scores, missing = _validate(batch, data)
            except LLMError:
                stats["llm_failures"] += 1
                scores, missing = {}, [a.canonical_url for a in batch]

            by_id = {a.canonical_url: a for a in batch}
            for item_id in missing:
                article = by_id[item_id]
                scores[item_id] = heuristic_enrich(
                    item_id, article.title, article.summary, article.source_weight
                )
                stats["heuristic_items"] += 1
            results.update(scores)

    await asyncio.gather(*(run_batch(b) for b in batches))
    return results, stats
