from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from sqlvec import bench, db
from sqlvec.config import ConfigError


def test_synthetic_dataset_shape_norm_and_determinism() -> None:
    d1, q1 = bench.synthetic_dataset(200, 10, 16, seed=1)
    d2, q2 = bench.synthetic_dataset(200, 10, 16, seed=1)
    assert d1.shape == (200, 16) and q1.shape == (10, 16)
    assert d1.dtype == np.float32
    assert np.allclose(np.linalg.norm(d1, axis=1), 1.0, atol=1e-5)
    assert np.array_equal(d1, d2) and np.array_equal(q1, q2)
    with pytest.raises(ValueError):
        bench.synthetic_dataset(0, 1, 1)


def test_exact_topk_matches_brute_force() -> None:
    data, queries = bench.synthetic_dataset(300, 7, 8, seed=3)
    got = bench.exact_topk(data, queries, 5, block=3)
    for qi, q in enumerate(queries):
        want = np.argsort(-(data @ q), kind="stable")[:5] + 1
        assert list(got[qi]) == list(want)


def test_exact_topk_k_larger_than_data() -> None:
    data, queries = bench.synthetic_dataset(3, 2, 4)
    assert bench.exact_topk(data, queries, 10).shape == (2, 3)


def test_recall_at_k() -> None:
    truth = np.array([[1, 2, 3], [4, 5, 6]])
    assert bench.recall_at_k([[1, 2, 3], [4, 5, 6]], truth, 3) == 1.0
    assert bench.recall_at_k([[3, 9, 1], [7, 8, 9]], truth, 3) == pytest.approx(1 / 3)
    assert bench.recall_at_k([], np.empty((0, 3)), 3) == 0.0
    with pytest.raises(ValueError):
        bench.recall_at_k([[1]], truth, 3)


def test_latency_stats() -> None:
    s = bench.latency_stats([0.001] * 19 + [0.1])
    assert s.p50_ms == pytest.approx(1.0) and s.p95_ms > 1.0
    assert bench.latency_stats([]).p50_ms == 0.0


def test_parse_spaceused() -> None:
    row = ("dbo.sqlvec_bench_items", "5000      ", "8136 KB", "7880 KB", "40 KB", "216 KB")
    s = bench.parse_spaceused(row)
    assert (s.rows, s.reserved_kb, s.data_kb, s.index_kb, s.unused_kb) == (
        5000,
        8136,
        7880,
        40,
        216,
    )
    with pytest.raises(ValueError):
        bench.parse_kb("12 MB")
    assert bench.parse_kb(None) == 0


def test_load_and_create_sql() -> None:
    sql = bench.load_sql(384)
    assert "OPENJSON(?)" in sql and "CAST(j.[value] AS VECTOR(384))" in sql and sql.count("?") == 2
    stmts = bench.create_table_sql(64)
    assert "PRIMARY KEY CLUSTERED" in stmts[1] and "VECTOR(64)" in stmts[1]
    with pytest.raises(ConfigError):
        bench.load_sql(0)


class _Responder:
    """Plays a SQL Server whose DiskANN is refused (like an engine without preview)."""

    def __init__(self, data: np.ndarray, refuse_ann: bool) -> None:
        self.data = data
        self.refuse_ann = refuse_ann
        self.loaded = 0

    def __call__(self, sql: str, params: tuple[Any, ...]) -> Any:
        if "SERVERPROPERTY('ProductVersion')" in sql:
            return [("17.0.4005.7", 3, "Developer Edition (64-bit)", "SQL Server 2025", 0)]
        if "CAST('[1,0]' AS VECTOR(2))" in sql:
            return [(1.0,)]
        if "PREVIEW_FEATURES'" in sql:
            return [(0,)]
        if "OPENJSON(?) AS j" in sql:
            self.loaded += len(json.loads(params[1]))
            return []
        if "sp_spaceused" in sql:
            return [("t", str(self.loaded), "100 KB", "90 KB", "8 KB", "2 KB")]
        if "VECTOR_DISTANCE('cosine', t.embedding, @qv)" in sql:
            q = np.asarray(json.loads(params[0]), dtype=np.float32)
            top = np.argsort(-(self.data @ q), kind="stable")[:10] + 1
            return [(int(i), 0.0) for i in top]
        if "CREATE VECTOR INDEX" in sql and self.refuse_ann:
            return RuntimeError("Vector index is a preview feature")
        return []


def test_run_benchmark_with_fake_engine(fake_conn) -> None:  # type: ignore[no-untyped-def]
    data, _ = bench.synthetic_dataset(250, 5, 8, seed=9)
    responder = _Responder(data, refuse_ann=True)
    conn = fake_conn(responder)
    result = bench.run_benchmark(conn, n=250, dims=8, queries=10, k=10, seed=9, batch=100)
    assert responder.loaded == 250
    assert len(conn.sql_matching("OPENJSON(?) AS j")) == 3  # 100 + 100 + 50
    assert result.exact_recall == 1.0
    assert result.ann_recall is None
    assert any("DiskANN index not available" in n for n in result.notes)
    assert conn.calls[-1][0] == f"DROP TABLE IF EXISTS {bench.BENCH_TABLE}"
    md = bench.to_markdown(result)
    assert "| exact (VECTOR_DISTANCE) | 1.000 |" in md
    assert "DiskANN (VECTOR_SEARCH)" not in md
    assert json.loads(result.to_json())["n"] == 250


def test_run_benchmark_rejects_engine_without_vectors(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "SERVERPROPERTY('ProductVersion')" in sql:
            return [("16.0.1", 3, "Developer", "SQL Server 2022", 0)]
        if "VECTOR(2)" in sql:
            return RuntimeError("Type VECTOR is not a defined system type")
        return []

    with pytest.raises(db.UnsupportedError):
        bench.run_benchmark(fake_conn(respond), n=100, dims=4, queries=10)


def test_markdown_includes_ann_row_when_measured() -> None:
    r = bench.BenchResult(
        n=1000, dims=8, queries=10, k=10, seed=1, clusters=4, noise=1.0,
        environment={"platform": "Azure SQL Database", "product_version": "12.0"},
        load_seconds=1.5, space_before_index=None,
        exact_latency=bench.LatencyStats(5.0, 6.0, 5.2), exact_recall=1.0,
        ann_build_seconds=2.0, ann_index_version="3", ann_syntax="SELECT TOP (k) WITH APPROXIMATE",
        ann_latency=bench.LatencyStats(1.0, 1.5, 1.1), ann_recall=0.97,
    )  # fmt: skip
    md = bench.to_markdown(r)
    assert "| DiskANN (VECTOR_SEARCH) | 0.970 | 1.00 | 1.50 | 1.10 |" in md
    assert "index version 3" in md
