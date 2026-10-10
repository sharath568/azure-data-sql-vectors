from __future__ import annotations

import math
from itertools import pairwise

import pytest

from sqlvec.rrf import DEFAULT_RRF_K, rrf_fuse, rrf_score


def test_default_k_is_60() -> None:
    assert DEFAULT_RRF_K == 60


def test_score_single_list() -> None:
    assert rrf_score([1]) == pytest.approx(1 / 61)
    assert rrf_score([10], k=60) == pytest.approx(1 / 70)


def test_score_sums_lists_and_ignores_missing() -> None:
    assert rrf_score([1, 3]) == pytest.approx(1 / 61 + 1 / 63)
    assert rrf_score([None, 2]) == pytest.approx(1 / 62)
    assert rrf_score([None, None]) == 0.0


@pytest.mark.parametrize("bad", [[0], [-1]])
def test_score_rejects_zero_based_ranks(bad: list[int]) -> None:
    with pytest.raises(ValueError):
        rrf_score(bad)


def test_negative_k_rejected() -> None:
    with pytest.raises(ValueError):
        rrf_score([1], k=-1)
    with pytest.raises(ValueError):
        rrf_fuse({"a": [1]}, k=-1)


def test_item_in_both_lists_beats_item_in_one() -> None:
    fused = rrf_fuse({"vector": [1, 2, 3], "fts": [3, 4]})
    assert fused[0].item == 3
    assert fused[0].ranks == {"vector": 3, "fts": 1}
    assert fused[0].score == pytest.approx(1 / 63 + 1 / 61)


def test_known_example_ordering() -> None:
    fused = rrf_fuse({"vector": ["a", "b", "c"], "fts": ["c", "a", "d"]})
    assert [f.item for f in fused] == ["a", "c", "b", "d"]
    # a: 1/61 + 1/62, c: 1/63 + 1/61
    assert fused[0].score > fused[1].score


def test_ties_broken_by_item_id() -> None:
    fused = rrf_fuse({"vector": [5], "fts": [2]})
    assert fused[0].score == fused[1].score
    assert [f.item for f in fused] == [2, 5]


def test_duplicates_within_a_list_count_once_at_best_rank() -> None:
    fused = rrf_fuse({"vector": [7, 7, 8]})
    by_item = {f.item: f for f in fused}
    assert by_item[7].ranks == {"vector": 1}
    assert by_item[8].ranks == {"vector": 2}


def test_limit_and_empty_inputs() -> None:
    assert rrf_fuse({}) == []
    assert rrf_fuse({"vector": [], "fts": []}) == []
    assert len(rrf_fuse({"v": list(range(1, 20))}, limit=5)) == 5


def test_k_zero_still_ranks_correctly() -> None:
    fused = rrf_fuse({"v": [1, 2]}, k=0)
    assert fused[0].score == pytest.approx(1.0)
    assert fused[1].score == pytest.approx(0.5)


def test_scores_are_monotonic_in_rank() -> None:
    scores = [rrf_score([r]) for r in range(1, 100)]
    assert all(a > b for a, b in pairwise(scores))
    assert all(math.isfinite(s) for s in scores)
