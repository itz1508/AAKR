"""SHADOW retrieval ranking metric tests (todo 11).

Acceptance:
- All metrics are pure functions with hand-computed expected values.
- Perfect ranking scores 1.0 across the board; degenerate cases (empty
  relevant set, no overlap) score 0.0 - never a silent perfect score.
- k-truncation is honored; recall counts only retrieved relevant items.

SHADOW PROFILE: these metrics are library-only and are never wired into
the /discover request path; enabling shadow evaluation of live retrieval
remains gated on the post-cutover soak.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aakr.evaluation import (
    f1_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


class RankingMetrics(unittest.TestCase):
    def test_perfect_ranking_scores_one_everywhere(self):
        ranked = ["a", "b", "c"]
        rel = {"a", "b"}
        self.assertEqual(precision_at_k(ranked, rel), 2 / 3)
        self.assertEqual(precision_at_k(ranked, rel, 2), 1.0)
        self.assertEqual(recall_at_k(ranked, rel), 1.0)
        self.assertEqual(f1_at_k(ranked, rel, 2), 1.0)
        self.assertEqual(reciprocal_rank(ranked, rel), 1.0)
        self.assertEqual(ndcg_at_k(ranked, rel), 1.0)  # a,b in ideal order

    def test_partial_overlap_hand_computed(self):
        # ranked [x, a, y, b]; relevant {a, b}
        ranked = ["x", "a", "y", "b"]
        rel = {"a", "b"}
        self.assertEqual(precision_at_k(ranked, rel), 0.5)
        self.assertEqual(precision_at_k(ranked, rel, 2), 0.5)
        self.assertEqual(recall_at_k(ranked, rel), 1.0)
        self.assertEqual(recall_at_k(ranked, rel, 2), 0.5)
        self.assertAlmostEqual(f1_at_k(ranked, rel), 2 * 0.5 * 1.0 / 1.5)
        self.assertEqual(reciprocal_rank(ranked, rel), 0.5)  # first relevant at rank 2
        dcg = 1 / math.log2(3) + 1 / math.log2(5)  # ranks 2 and 4
        idcg = 1 / math.log2(2) + 1 / math.log2(3)  # ideal ranks 1 and 2
        self.assertAlmostEqual(ndcg_at_k(ranked, rel), dcg / idcg)

    def test_no_overlap_and_empty_cases_score_zero(self):
        ranked = ["x", "y"]
        self.assertEqual(precision_at_k(ranked, {"a"}), 0.0)
        self.assertEqual(recall_at_k(ranked, {"a"}), 0.0)
        self.assertEqual(f1_at_k(ranked, {"a"}), 0.0)
        self.assertEqual(reciprocal_rank(ranked, {"a"}), 0.0)
        self.assertEqual(ndcg_at_k(ranked, {"a"}), 0.0)
        # empty relevant set: degenerate query, NOT a perfect score
        for fn in (lambda: precision_at_k(ranked, set()),
                   lambda: recall_at_k(ranked, set()),
                   lambda: f1_at_k(ranked, set()),
                   lambda: reciprocal_rank(ranked, set()),
                   lambda: ndcg_at_k(ranked, set())):
            self.assertEqual(fn(), 0.0)
        # empty ranking
        self.assertEqual(precision_at_k([], {"a"}), 0.0)
        self.assertEqual(reciprocal_rank([], {"a"}), 0.0)
        self.assertEqual(ndcg_at_k([], {"a"}), 0.0)
        # k <= 0
        self.assertEqual(precision_at_k(ranked, {"a"}, 0), 0.0)
        self.assertEqual(ndcg_at_k(ranked, {"a"}, 0), 0.0)

    def test_k_truncation_bounds_and_late_hits(self):
        ranked = ["x", "y", "z", "a"]
        rel = {"a"}
        self.assertEqual(precision_at_k(ranked, rel, 3), 0.0)  # hit beyond k
        self.assertEqual(recall_at_k(ranked, rel, 3), 0.0)
        self.assertEqual(recall_at_k(ranked, rel), 1.0)
        self.assertEqual(reciprocal_rank(ranked, rel), 0.25)
        self.assertAlmostEqual(ndcg_at_k(ranked, rel), (1 / math.log2(5)) / 1.0)

    def test_duplicates_never_count_as_multiple_hits(self):
        # a duplicate id is not two relevant retrievals: unique-hit semantics
        ranked = ["a", "a", "x"]
        rel = {"a"}
        self.assertEqual(precision_at_k(ranked, rel, 3), 1 / 3)
        self.assertEqual(recall_at_k(ranked, rel), 1.0)  # a is retrieved
        self.assertEqual(reciprocal_rank(ranked, rel), 1.0)
        self.assertEqual(ndcg_at_k(ranked, rel, 2), 1.0)  # a at ideal rank 1
        self.assertLessEqual(recall_at_k(["a"] * 5, rel), 1.0)

    def test_duplicate_padding_cannot_inflate_scores(self):
        # spamming the ranking with duplicates of a hit does not help
        ranked = ["a"] * 10 + ["b"]
        rel = {"a", "b"}
        self.assertEqual(recall_at_k(ranked, rel, 5), 0.5)
        self.assertLess(ndcg_at_k(ranked, rel, 5), 1.0)


if __name__ == "__main__":
    unittest.main()
