"""Integration tests against a real SQL Server 2025 / Azure SQL database.

Run with SQLVEC_TEST_CONN set (see tests/conftest.py). Everything that is a
preview feature (DiskANN / VECTOR_SEARCH) or an optional component
(full-text search) is detected first and *skipped with the engine's reason*
if unavailable, so CI stays green without pretending a feature works.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sqlvec import bench, db, vecjson
from sqlvec.embeddings import HashEmbedder
from sqlvec.ingest import ingest_path
from sqlvec.rrf import rrf_fuse

pytestmark = pytest.mark.integration

TEST_CONN = os.environ.get("SQLVEC_TEST_CONN")
DIMS = 64
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "docs"


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert TEST_CONN
    db.create_database(TEST_CONN)
    c = db.connect(TEST_CONN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="module")
def caps(conn: Any) -> db.Capabilities:
    caps = db.detect_capabilities(conn)
    print(f"\nengine: {caps.platform} {caps.product_version}; fulltext={caps.fulltext_installed}")
    if not caps.vector_type:
        pytest.skip(f"{caps.platform} has no VECTOR type: {caps.notes}")
    return caps


@pytest.fixture(scope="module")
def loaded(conn: Any, caps: db.Capabilities) -> db.InitReport:
    db.drop_schema(conn)
    report = db.init_schema(conn, DIMS, embedder_name=f"hash-{DIMS}")
    ingest_path(conn, EXAMPLES, HashEmbedder(DIMS), dims=DIMS, chunk_size=400, overlap=60)
    if report.fulltext_index:
        assert db.wait_for_fulltext(conn, timeout=90)
    return report


# --- basics -------------------------------------------------------------------


def test_vector_json_round_trip(conn: Any, caps: db.Capabilities) -> None:
    vec = np.random.default_rng(1).standard_normal(DIMS).astype(np.float32)
    row = db.fetchone(
        conn, f"SELECT CAST(CAST(? AS VECTOR({DIMS})) AS NVARCHAR(MAX))", (vecjson.to_json(vec),)
    )
    assert row is not None
    back = np.asarray(vecjson.from_json(row[0]), dtype=np.float32)
    assert np.allclose(back, vec, rtol=1e-6, atol=1e-7)


def test_vector_distance_matches_numpy(conn: Any, caps: db.Capabilities) -> None:
    rng = np.random.default_rng(2)
    a, b = rng.standard_normal((2, DIMS)).astype(np.float32)
    row = db.fetchone(
        conn,
        f"DECLARE @a VECTOR({DIMS}) = CAST(? AS VECTOR({DIMS})); "
        f"DECLARE @b VECTOR({DIMS}) = CAST(? AS VECTOR({DIMS})); "
        "SELECT VECTOR_DISTANCE('cosine', @a, @b), VECTOR_DISTANCE('euclidean', @a, @b), "
        "VECTOR_DISTANCE('dot', @a, @b)",
        (vecjson.to_json(a), vecjson.to_json(b)),
    )
    assert row is not None
    cos = 1 - float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))
    assert float(row[0]) == pytest.approx(cos, abs=1e-5)
    assert float(row[1]) == pytest.approx(float(np.linalg.norm(a - b)), rel=1e-5)
    assert float(row[2]) == pytest.approx(-float(a @ b), rel=1e-5)  # negative dot product


def test_init_is_idempotent_and_guards_dims(conn: Any, loaded: db.InitReport) -> None:
    again = db.init_schema(conn, DIMS)
    assert again.applied[:3] == ["001_schema.sql", "002_fulltext.sql", "003_exact_search.sql"]
    with pytest.raises(db.SchemaError):
        db.init_schema(conn, DIMS * 2)


def test_ingest_counts_and_skip_unchanged(conn: Any, loaded: db.InitReport) -> None:
    totals = db.counts(conn)
    assert totals["documents"] == len(list(EXAMPLES.glob("*.md")))
    assert totals["chunks"] >= totals["documents"]
    report = ingest_path(conn, EXAMPLES, HashEmbedder(DIMS), dims=DIMS, chunk_size=400, overlap=60)
    assert report.files_ingested == 0 and report.files_unchanged == totals["documents"]


# --- exact search -----------------------------------------------------------------


def _stored_vectors(conn: Any) -> dict[int, np.ndarray]:
    rows = db.fetchall(
        conn, "SELECT id, CAST(embedding AS NVARCHAR(MAX)) FROM dbo.sqlvec_chunks ORDER BY id"
    )
    return {int(r[0]): np.asarray(vecjson.from_json(r[1]), dtype=np.float32) for r in rows}


def test_exact_search_matches_numpy(conn: Any, loaded: db.InitReport) -> None:
    emb = HashEmbedder(DIMS)
    q = np.asarray(emb.embed_one("pause the database when it is idle"), dtype=np.float32)
    stored = _stored_vectors(conn)
    dist = {
        i: 1 - float(v @ q) / (float(np.linalg.norm(v)) * float(np.linalg.norm(q)))
        for i, v in stored.items()
    }

    res = db.search_exact(conn, q.tolist(), DIMS, k=5)
    assert len(res) == 5
    got = [r.distance for r in res]
    assert got == sorted(got)  # type: ignore[type-var]
    for r in res:  # each distance is what NumPy says for that row
        assert r.distance == pytest.approx(dist[r.chunk_id], abs=1e-5)
    # and they are the 5 smallest (compare values, so ties can't flake the test)
    assert got == pytest.approx(sorted(dist.values())[:5], abs=1e-5)


def test_metadata_filter(conn: Any, loaded: db.InitReport) -> None:
    q = HashEmbedder(DIMS).embed_one("cost")
    md_only = db.search_exact(conn, q, DIMS, k=50, filters={"ext": "md"})
    assert md_only and all(r.source.endswith(".md") for r in md_only)
    assert db.search_exact(conn, q, DIMS, k=5, filters={"ext": "nope"}) == []
    evil = db.search_exact(conn, q, DIMS, k=5, filters={"ext": "md' OR 1=1 --"})
    assert evil == []


# --- full-text and hybrid ----------------------------------------------------------


def _need_fulltext(report: db.InitReport) -> None:
    if not report.fulltext_index:
        pytest.skip("full-text search is not installed on this server (mssql-server-fts)")


def test_fts_finds_exact_token(conn: Any, loaded: db.InitReport) -> None:
    _need_fulltext(loaded)
    res: list[db.SearchResult] = []
    for _ in range(20):  # population is asynchronous; give it a moment
        res = db.search_fts(conn, "autopause", k=3)
        if res:
            break
        time.sleep(1)
    assert res, "full-text index never returned the token"
    assert all("auto" in r.content.lower() for r in res)


def test_hybrid_matches_python_rrf(conn: Any, loaded: db.InitReport) -> None:
    _need_fulltext(loaded)
    query = "how do I stop paying when the database is idle"
    qvec = HashEmbedder(DIMS).embed_one(query)
    candidates = 20

    vec_ids = [r.chunk_id for r in db.search_exact(conn, qvec, DIMS, k=candidates)]
    fts_rows = db.fetchall(
        conn,
        "SELECT TOP (?) c.id FROM FREETEXTTABLE(dbo.sqlvec_chunks, content, ?) AS ft "
        "JOIN dbo.sqlvec_chunks AS c ON c.id = ft.[KEY] ORDER BY ft.[RANK] DESC, c.id",
        (candidates, query),
    )
    fts_ids = [int(r[0]) for r in fts_rows]
    expected = rrf_fuse({"vector": vec_ids, "fts": fts_ids}, k=60, limit=5)

    got = db.search_hybrid(conn, query, qvec, DIMS, k=5, candidates=candidates)
    assert [r.chunk_id for r in got] == [e.item for e in expected]
    for g, e in zip(got, expected, strict=True):
        assert g.score == pytest.approx(e.score, abs=1e-12)
        assert g.vector_rank == e.ranks.get("vector")
        assert g.fts_rank == e.ranks.get("fts")


# --- DiskANN (preview on SQL Server 2025) -----------------------------------------------


def test_diskann_index_and_vector_search(conn: Any, caps: db.Capabilities) -> None:
    data, queries = bench.synthetic_dataset(400, 20, DIMS, seed=5, clusters=8)
    truth = bench.exact_topk(data, queries, 10)
    for stmt in bench.create_table_sql(DIMS):
        db.run(conn, stmt)
    db.run(conn, bench.load_sql(DIMS), (1, vecjson.many_to_json(data)))
    try:
        preview = db.enable_preview_features(conn, caps)
        try:
            db.run(conn, bench.CREATE_INDEX_SQL)
        except Exception as exc:
            pytest.skip(f"DiskANN not available on {caps.platform} ({preview}): {exc}")
        info = db.vector_index_info(conn, bench.BENCH_TABLE)
        assert info is not None and (info.metric or "").lower() == "cosine"
        print(f"\nDiskANN index version: {info.version}")

        found = []
        legacy_used = None
        for q in queries:
            hits, legacy_used = db.ann_ids(conn, bench.BENCH_TABLE, q, DIMS, 10)
            assert len(hits) == 10
            assert [h[1] for h in hits] == sorted(h[1] for h in hits)
            found.append([h[0] for h in hits])
        recall = bench.recall_at_k(found, truth, 10)
        print(f"DiskANN recall@10 on 400 x {DIMS}: {recall:.3f} (legacy syntax: {legacy_used})")
        assert recall >= 0.8
    finally:
        db.run(conn, f"DROP TABLE IF EXISTS {bench.BENCH_TABLE}")


def test_small_benchmark_runs(conn: Any, caps: db.Capabilities) -> None:
    result = bench.run_benchmark(conn, n=300, dims=16, queries=20, k=10, seed=3)
    assert result.exact_recall == pytest.approx(1.0, abs=0.02)
    assert result.space_before_index is not None and result.space_before_index.rows == 300
    md = bench.to_markdown(result)
    assert "exact (VECTOR_DISTANCE)" in md
    json.loads(result.to_json())
