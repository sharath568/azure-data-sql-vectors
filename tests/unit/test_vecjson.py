from __future__ import annotations

import json

import numpy as np
import pytest

from sqlvec.vecjson import VectorFormatError, from_json, many_to_json, to_json


def test_basic_format_is_compact_json() -> None:
    assert to_json([0.5, 1, -2.25]) == "[0.5,1,-2.25]"
    assert json.loads(to_json([0.1, 2, 30])) == pytest.approx([0.1, 2, 30], rel=1e-7)


def test_float32_round_trip_is_exact() -> None:
    rng = np.random.default_rng(7)
    vec = rng.standard_normal(1536).astype(np.float32)
    back = np.asarray(from_json(to_json(vec)), dtype=np.float32)
    assert np.array_equal(vec, back)


def test_tiny_and_huge_values_stay_valid_json() -> None:
    text = to_json([1e-30, -3.4e38, 0.0, -0.0])
    assert json.loads(text)[0] == pytest.approx(1e-30, rel=1e-6)


def test_dims_check() -> None:
    with pytest.raises(VectorFormatError, match="expected 3"):
        to_json([1.0, 2.0], dims=3)
    assert to_json([1.0, 2.0, 3.0], dims=3) == "[1,2,3]"


@pytest.mark.parametrize("bad", [[float("nan")], [1.0, float("inf")], []])
def test_rejects_non_finite_and_empty(bad: list[float]) -> None:
    with pytest.raises(VectorFormatError):
        to_json(bad)


def test_many_to_json_is_array_of_arrays() -> None:
    text = many_to_json(np.eye(2, dtype=np.float32))
    assert json.loads(text) == [[1, 0], [0, 1]]


def test_from_json_validation() -> None:
    assert from_json("[1, 2.5]") == [1.0, 2.5]
    for bad in ["", "{}", "[]", '["a"]', "[true]", "not json"]:
        with pytest.raises(VectorFormatError):
            from_json(bad)


def test_accepts_numpy_2d_row_and_lists() -> None:
    assert to_json(np.array([[1.0, 2.0]])) == "[1,2]"
