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
    "【核心准则】这名读者只看技术。唯一的判断标准是："
    "读完能否获得可迁移的技术认知——新方法、新模型能力、新工具、新的失败教训。\n"
    "\n"
    "70 分以上：含有具体技术事实的内容。\n"
    "- 新模型、新架构、新训练或推理方法，且给出了技术细节或评测数字；\n"
    "- 论文解读，或对方法原理的系统梳理；\n"
    "- 能直接用在工程里的开源工具、库、内核优化；\n"
    "- 踩坑复盘、性能优化、可复现的失败教训。\n"
    "\n"
    "40-70 分：有技术信息量，但没有新的技术事实，例如对已有方法的比较、评测复现。\n"
    "\n"
    "30 分以下（一律，无论措辞多宏大）：\n"
    "- 产业新闻：融资、营收、财报、人事、合作、诉讼、政策；\n"
    "- 产品公告：功能上线、区域开放、促销、客户案例、企业签约；\n"
    "- 纯版本号发布说明，除非它引入了新模型或新算法；\n"
    "- 与 AI 无关的内容：前端/数据库工具、安全公告、基础设施故障；\n"
    "- 招聘、活动、广告、社会责任、公益项目。\n"
    "\n"
    "注意：标题出现「发布」「推出」「上线」并不代表它有技术内容，"
    "要判断里面是否包含可迁移的技术信息，而不是看公关语气。\n"
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
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[dict[str, ItemScore], dict[str, int]]:
    """Enrich articles in batches. Never raises: failures degrade to heuristics.

    ``system_prompt`` is overridable because the evaluation harness needs a
    deliberately *neutral* summarisation for its labelling UI -- see
    :mod:`ai_digest.eval.annotate`.
    """
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
                    system=system_prompt, user=build_user_prompt(batch)
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
