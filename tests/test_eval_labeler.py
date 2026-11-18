from __future__ import annotations

import json

from ai_digest.eval.annotate import ANNOTATE_SYSTEM_PROMPT, annotate_case
from ai_digest.eval.dataset import (
    AnnotatedItem,
    CaseAnnotations,
    EvalCase,
    EvalItem,
    annotations_path,
)
from ai_digest.eval.labeler import build_labeler_html
from ai_digest.llm.fake import FakeLLM
from ai_digest.process.rank import SYSTEM_PROMPT as RANKING_PROMPT


def make_case() -> EvalCase:
    return EvalCase(
        case_id="2026-11-18",
        items=[
            EvalItem(
                id="https://a.example/1",
                title="OpenAI releases a faster inference stack",
                source="Feed A",
                summary="A new kernel cuts latency by 40 percent on H100 GPUs.",
            ),
            EvalItem(
                id="https://b.example/2",
                title="We are hiring a research engineer",
                source="Feed B",
                summary="Join the team in San Francisco.",
            ),
        ],
    )


async def test_annotate_covers_every_item(settings) -> None:
    case = make_case()
    annotations = await annotate_case(case, settings, llm=FakeLLM())

    assert set(annotations.items) == set(case.ids())
    assert all(gist.one_liner for gist in annotations.items.values())
    assert annotations.coverage(case) == 1.0


async def test_annotate_uses_a_neutral_prompt(settings) -> None:
    """The gist must not carry the ranker's opinion, or labels get anchored."""

    class RecordingLLM(FakeLLM):
        def __init__(self) -> None:
            super().__init__()
            self.systems: list[str] = []

        async def complete_json(self, *, system: str, user: str) -> dict:
            self.systems.append(system)
            return await super().complete_json(system=system, user=user)

    llm = RecordingLLM()
    await annotate_case(make_case(), settings, llm=llm)

    assert llm.systems, "no LLM call recorded"
    assert all(system == ANNOTATE_SYSTEM_PROMPT for system in llm.systems)
    # And the prompt itself must forbid evaluative wording.
    assert "不要评价重要性" in ANNOTATE_SYSTEM_PROMPT
    assert "不要判断是否值得读" in ANNOTATE_SYSTEM_PROMPT
    assert ANNOTATE_SYSTEM_PROMPT != RANKING_PROMPT


def test_annotated_item_has_no_score_field() -> None:
    # Guard against someone "helpfully" adding the score back into the page:
    # an annotator who can see 95/100 is no longer labelling independently.
    assert "score" not in AnnotatedItem.model_fields


def test_labeler_renders_chinese_gist() -> None:
    case = make_case()
    annotations = CaseAnnotations(
        case_id=case.case_id,
        items={
            "https://a.example/1": AnnotatedItem(
                one_liner="OpenAI 发布更快的推理栈", bullets=["延迟降低 40%"], topic="infra"
            ),
            "https://b.example/2": AnnotatedItem(
                one_liner="招聘研究工程师", bullets=[], topic="other"
            ),
        },
    )

    html = build_labeler_html(case, annotations)

    assert "OpenAI 发布更快的推理栈" in html
    assert "延迟降低 40%" in html
    assert "招聘研究工程师" in html
    # The English headline is still there, demoted.
    assert "OpenAI releases a faster inference stack" in html
    assert "整页都没有中文摘要" not in html


def test_labeler_warns_when_annotations_missing() -> None:
    html = build_labeler_html(make_case(), None)
    assert "整页都没有中文摘要" in html
    assert "eval annotate" in html


def test_labeler_warns_on_partial_coverage() -> None:
    case = make_case()
    annotations = CaseAnnotations(
        case_id=case.case_id,
        items={"https://a.example/1": AnnotatedItem(one_liner="有摘要")},
    )
    html = build_labeler_html(case, annotations)
    assert "1 条中文摘要缺失" in html


def test_embedded_items_carry_no_score() -> None:
    case = make_case()
    annotations = CaseAnnotations(
        case_id=case.case_id,
        items={"https://a.example/1": AnnotatedItem(one_liner="摘要", topic="infra")},
    )
    html = build_labeler_html(case, annotations)

    start = html.index("const ITEMS =") + len("const ITEMS =")
    end = html.index(";", start)
    items = json.loads(html[start:end])
    assert all("score" not in item for item in items)


def test_annotations_path_sits_beside_case(tmp_path) -> None:
    case_path = tmp_path / "cases" / "2026-11-18.json"
    assert annotations_path(case_path).name == "2026-11-18.zh.json"
    assert annotations_path(case_path).parent == case_path.parent


# -- rubric: forced choice, not "is this worth reading" --------------------


def test_rubric_asks_for_a_forced_choice() -> None:
    html = build_labeler_html(make_case(), None, slots=12)
    assert "这条必须占一个位置吗" in html
    assert "只有 12 个位置" in html
    assert "必须留" in html
    assert "可以不看" in html


def test_rubric_storage_is_versioned() -> None:
    """v1 ('worth reading') labels must not resurface under the v2 question."""
    html = build_labeler_html(make_case(), None, slots=12)
    assert "aidigest-labels-rubric2-" in html
    assert 'aidigest-labels-" ' not in html


def test_export_filename_is_versioned() -> None:
    html = build_labeler_html(make_case(), None, slots=12)
    assert '-rubric2.json' in html


def test_slot_budget_is_rendered() -> None:
    html = build_labeler_html(make_case(), None, slots=7)
    assert "必须留 0 / 7" in html
    assert "const SLOTS = 7;" in html
