"""Reciprocal Rank Fusion, reference implementation in Python.

The production path is the stored procedure ``dbo.sqlvec_search_hybrid``
(see ``sql/004_hybrid_search.sql``). This module exists so the maths is easy
to read and test, and so integration tests can check that the T-SQL gets the
same answer.

For each result list L and each item d in it (rank starting at 1)::

    score(d) += 1 / (k + rank_L(d))

Items are returned by descending score. Ties are broken by item id, which is
also what the stored procedure does.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

DEFAULT_RRF_K = 60

T = TypeVar("T", bound=Hashable)


@dataclass(frozen=True)
class FusedResult(Generic[T]):
    """One fused item with its total score and its rank in each input list."""

    item: T
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


def rrf_score(ranks: Sequence[int | None], k: int = DEFAULT_RRF_K) -> float:
    """RRF score for one item given its 1-based rank in each list (None = absent)."""
    if k < 0:
        raise ValueError("k must be >= 0")
    total = 0.0
    for rank in ranks:
        if rank is None:
            continue
        if rank < 1:
            raise ValueError("ranks are 1-based")
        total += 1.0 / (k + rank)
    return total


def rrf_fuse(
    ranked_lists: Mapping[str, Sequence[T]],
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[FusedResult[T]]:
    """Fuse several ranked lists into one.

    Args:
        ranked_lists: name -> items ordered best first, e.g.
            ``{"vector": [3, 1, 7], "fts": [1, 9]}``. Duplicates within one
            list are ignored after their first (best) position.
        k: the RRF constant. 60 is the common default.
        limit: return at most this many results.
    """
    if k < 0:
        raise ValueError("k must be >= 0")
    ranks: dict[T, dict[str, int]] = {}
    for name, items in ranked_lists.items():
        seen: set[T] = set()
        position = 0
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            position += 1
            ranks.setdefault(item, {})[name] = position

    fused = [
        FusedResult(item=item, score=rrf_score(list(r.values()), k), ranks=r)
        for item, r in ranks.items()
    ]
    fused.sort(key=lambda fr: (-fr.score, _sort_key(fr.item)))
    return fused[:limit] if limit is not None else fused


def _sort_key(item: object) -> tuple[int, object]:
    # Numbers sort numerically and before strings; anything else by repr.
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return (0, item)
    if isinstance(item, str):
        return (1, item)
    return (2, repr(item))
