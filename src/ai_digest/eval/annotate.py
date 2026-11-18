"""Generate Chinese gists for a frozen case, so labelling is about judgement.

Labelling from English headlines means the labeller is partly grading their own
reading speed. A one-line Chinese gist plus a couple of bullets lets the
decision be about the content instead.

This runs the *same* enrichment code as production, but over **every** candidate
rather than the prefiltered shortlist -- items the ranker would have dropped
still need a gist if the human is going to judge them.

Scores are discarded on purpose. See :class:`AnnotatedItem`.
"""

from __future__ import annotations

from ..llm.base import LLMClient, build_llm
from ..process.rank import enrich_articles
from .dataset import AnnotatedItem, CaseAnnotations, EvalCase
from .runner import articles_from_case

# A neutral summarisation prompt, deliberately different from the ranking one.
# If the gist says "与 AI 技术无关" or "值得重点关注", the annotator is no longer
# judging the content -- they are agreeing with the model. That would inflate
# every metric measured on these labels.
ANNOTATE_SYSTEM_PROMPT = (
    "你是一名技术编辑，正在为人工评审准备材料。只输出 JSON，不要输出解释文字。\n"
    "对每篇文章给出三个字段：\n"
    "- one_liner：客观说明这篇文章在讲什么，不超过 60 个汉字；\n"
    "- bullets：2-3 条具体要点，尽量包含原文中的数字或技术细节，每条不超过 40 个汉字；\n"
    "- topic：只能从 models, agents, research, infra, tools, industry, other 中选一个。\n"
    "\n"
    "严格要求：只陈述内容。不要评价重要性，不要判断是否值得读，"
    "不要出现「值得关注」「重要」「无关」「属于……而非……」这类判断性措辞。"
    "读者会自己判断。"
)


async def annotate_case(
    case: EvalCase, settings, *, llm: LLMClient | None = None
) -> CaseAnnotations:
    llm = llm or build_llm(settings)
    articles = articles_from_case(case)

    scores, _stats = await enrich_articles(
        articles,
        llm,
        batch_size=settings.llm_batch_size,
        concurrency=settings.llm_concurrency,
        system_prompt=ANNOTATE_SYSTEM_PROMPT,
    )

    items: dict[str, AnnotatedItem] = {}
    for article in articles:
        score = scores.get(article.canonical_url)
        if score is None:
            continue
        items[article.canonical_url] = AnnotatedItem(
            one_liner=score.one_liner,
            bullets=score.bullets,
            topic=score.topic,
        )

    return CaseAnnotations(
        case_id=case.case_id,
        provider=getattr(llm, "name", "unknown"),
        items=items,
    )
