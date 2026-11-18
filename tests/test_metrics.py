from __future__ import annotations

import pytest

from ai_digest.eval.metrics import (
    evaluate_ranking,
    mean_score_by_relevance,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

# 10 items; 1,2,3 relevant; 4..10 not.
LABELS = {f"i{i}": (1 if i <= 3 else 0) for i in range(1, 11)}


class TestPrecision:
    def test_perfect_top_k(self) -> None:
        ranked = ["i1", "i2", "i3", "i4"]
        assert precision_at_k(ranked, LABELS, 3) == 1.0

    def test_half_hits(self) -> None:
        ranked = ["i1", "i4", "i2", "i5"]
        assert precision_at_k(ranked, LABELS, 4) == 0.5

    def test_uses_returned_length_when_pool_smaller_than_k(self) -> None:
        ranked = ["i1", "i4"]
        # 1 of 2 returned is relevant -> 0.5, not 1/3
        assert precision_at_k(ranked, LABELS, 12) == 0.5

    def test_empty(self) -> None:
        assert precision_at_k([], LABELS, 5) == 0.0


class TestRecall:
    def test_all_relevant_found(self) -> None:
        assert recall_at_k(["i1", "i2", "i3"], LABELS, 3) == 1.0

    def test_one_of_three(self) -> None:
        assert recall_at_k(["i1", "i4", "i5"], LABELS, 3) == pytest.approx(1 / 3)

    def test_no_relevant_in_labels(self) -> None:
        assert recall_at_k(["i1"], {"i1": 0}, 5) == 0.0


class TestNdcg:
    def test_ideal_ordering_is_one(self) -> None:
        assert ndcg_at_k(["i1", "i2", "i3", "i4"], LABELS, 4) == pytest.approx(1.0)

    def test_relevant_items_buried_scores_lower(self) -> None:
        ideal = ndcg_at_k(["i1", "i2", "i3", "i4"], LABELS, 4)
        buried = ndcg_at_k(["i4", "i5", "i6", "i1"], LABELS, 4)
        assert buried < ideal

    def test_position_matters_beyond_top_k(self) -> None:
        # Both put exactly 2 relevant items in the top 4, so precision ties,
        # but NDCG must prefer the one that puts them earlier.
        early = ndcg_at_k(["i1", "i2", "i4", "i5"], LABELS, 4)
        late = ndcg_at_k(["i4", "i5", "i1", "i2"], LABELS, 4)
        assert early > late

    def test_no_relevant_labels(self) -> None:
        assert ndcg_at_k(["a", "b"], {"a": 0, "b": 0}, 2) == 0.0


class TestMeanScore:
    def test_relevant_scores_higher(self) -> None:
        scores = {"i1": 90, "i2": 85, "i4": 40, "i5": 30}
        result = mean_score_by_relevance(
            ["i1", "i2", "i4", "i5"], scores, {"i1": 1, "i2": 1, "i4": 0, "i5": 0}
        )
        assert result["mean_score_relevant"] == 87.5
        assert result["mean_score_irrelevant"] == 35.0
        assert result["n_relevant"] == 2
        assert result["n_irrelevant"] == 2


class TestEvaluateRanking:
    def test_reports_all_requested_keys(self) -> None:
        result = evaluate_ranking(
            ["i1", "i2", "i4"], LABELS, k=3, scores={"i1": 9, "i2": 8, "i4": 4}
        )
        assert result["precision@3"] == pytest.approx(0.667, abs=1e-3)
        assert result["recall@3"] == pytest.approx(0.667, abs=1e-3)
        assert result["relevant_total"] == 3
        assert result["labelled"] == 10
        assert "mean_score_relevant" in result

    def test_unlabelled_items_count_as_irrelevant(self) -> None:
        # Documents the trade-off: partial labelling makes precision pessimistic.
        result = evaluate_ranking(["i1", "zzz"], {"i1": 1}, k=2)
        assert result["precision@2"] == 0.5


class TestLiftOverRandom:
    def test_random_baseline_reflects_relevance_rate(self) -> None:
        # 3 of 10 relevant -> random precision is 0.3 at any k.
        result = evaluate_ranking(["i1", "i2", "i4"], LABELS, k=3)
        assert result["random_precision@3"] == pytest.approx(0.3)

    def test_lift_is_precision_over_random(self) -> None:
        # Top 3 are all relevant -> precision 1.0, lift 1/0.3 = 3.333
        result = evaluate_ranking(["i1", "i2", "i3"], LABELS, k=3)
        assert result["lift_over_random"] == pytest.approx(3.333, abs=1e-3)

    def test_liberal_labels_shrink_the_lift(self) -> None:
        # Same perfect ranking, but if most of the pool is "relevant" the
        # ranking is doing much less work.
        liberal = {f"i{i}": (1 if i <= 9 else 0) for i in range(1, 11)}
        strict = {f"i{i}": (1 if i <= 2 else 0) for i in range(1, 11)}
        ranked = ["i1", "i2", "i3"]

        liberal_lift = evaluate_ranking(ranked, liberal, k=3)["lift_over_random"]
        strict_lift = evaluate_ranking(ranked, strict, k=3)["lift_over_random"]
        assert liberal_lift < strict_lift
