from __future__ import annotations

import pytest

from ai_digest.eval.runner import aggregate, render_summary_markdown


def make_report(case_id: str, precision: float, lift: float, relevant: int) -> dict:
    return {
        "case_id": case_id,
        "metrics": {
            "precision@12": precision,
            "ndcg@12": 0.8,
            "lift_over_random": lift,
            "relevant_total": relevant,
        },
    }


class TestAggregate:
    def test_macro_average_gives_each_case_one_vote(self) -> None:
        # A case with 25 relevant items must not outweigh one with 3.
        reports = [
            make_report("a", 0.5, 1.0, 25),
            make_report("b", 1.0, 3.0, 3),
        ]
        totals = aggregate(reports, 12)

        assert totals["cases"] == 2
        assert totals["mean_precision@12"] == pytest.approx(0.75)
        assert totals["mean_lift"] == pytest.approx(2.0)
        assert totals["total_relevant"] == 28

    def test_min_and_max_lift_expose_instability(self) -> None:
        reports = [
            make_report("a", 0.5, 1.1, 5),
            make_report("b", 0.9, 4.2, 5),
        ]
        totals = aggregate(reports, 12)

        # The mean hides that one day was barely better than random.
        assert totals["min_lift"] == 1.1
        assert totals["max_lift"] == 4.2

    def test_empty(self) -> None:
        assert aggregate([], 12) == {"cases": 0, "k": 12}


class TestSummaryMarkdown:
    def test_contains_totals_and_one_row_per_case(self) -> None:
        reports = [
            make_report("2026-11-18", 0.75, 2.562, 12),
            make_report("2026-11-19", 0.58, 1.8, 20),
        ]
        markdown = render_summary_markdown(reports, aggregate(reports, 12), 12)

        assert "| case |" in markdown
        assert "2026-11-18" in markdown
        assert "2026-11-19" in markdown
        assert "2.562" in markdown
        assert markdown.count("| 2026-") == 2
