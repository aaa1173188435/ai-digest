"""Markdown rendering.

Useful for local inspection, and for anyone who wants to pipe the digest into a
chat tool or a note app. Deliberately plain: no emoji headers, no ASCII art.
"""

from __future__ import annotations

from ..models import Digest

TOPIC_LABEL = {
    "models": "模型",
    "agents": "Agent",
    "research": "研究",
    "infra": "基础设施",
    "tools": "工具",
    "industry": "产业",
    "other": "其他",
}


def render_markdown(digest: Digest) -> str:
    lines: list[str] = [f"# AI Digest · {digest.digest_date.isoformat()}", ""]

    if not digest.items:
        lines += ["_今天没有新内容。_", ""]
    else:
        for item in digest.items:
            topic = TOPIC_LABEL.get(item.topic, item.topic)
            also = (
                f" · 另见 {'、'.join(item.also_reported_by)}"
                if item.also_reported_by
                else ""
            )
            lines.append(f"## {item.rank}. {item.title}")
            lines.append("")
            lines.append(f"`{item.source}` · {topic} · 评分 {item.score:.0f}{also}")
            lines.append("")
            if item.one_liner:
                lines.append(item.one_liner)
                lines.append("")
            for bullet in item.bullets:
                lines.append(f"- {bullet}")
            if item.bullets:
                lines.append("")
            lines.append(item.url)
            lines.append("")

    stats = digest.stats
    lines += [
        "---",
        "",
        (
            f"候选 {stats.get('candidates', 0)} 篇，去重后 "
            f"{stats.get('after_dedupe', 0)} 条，精选 {len(digest.items)} 条；"
            f"模型 {stats.get('llm_provider', '-')}，"
            f"预估成本 ${stats.get('estimated_cost_usd', 0):.4f}，"
            f"耗时 {stats.get('duration_seconds', 0)}s。"
        ),
        "",
    ]
    return "\n".join(lines)
