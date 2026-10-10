"""db.py against a fake connection: parameterization, capability handling, fallbacks."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from sqlvec import db
from sqlvec.config import ConfigError

EVIL = "x'); DROP TABLE dbo.sqlvec_chunks; --"


def caps_responder(
    *,
    engine: int = 3,
    version: str = "17.0.1000.7",
    fulltext: int = 1,
    vector_ok: bool = True,
    preview: int | None = 0,
    extra: Callable[[str, tuple[Any, ...]], Any] | None = None,
) -> Callable[[str, tuple[Any, ...]], Any]:
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if extra is not None:
            out = extra(sql, params)
            if out is not None:
                return out
        if "SERVERPROPERTY('ProductVersion')" in sql:
            return [
                (
                    version,
                    engine,
                    "Developer Edition (64-bit)",
                    "Microsoft SQL Server 2025",
                    fulltext,
                )
            ]
        if "CAST('[1,0]' AS VECTOR(2))" in sql:
            return (
                [(1.0,)] if vector_ok else RuntimeError("Type VECTOR is not a defined system type")
            )
        if "PREVIEW_FEATURES'" in sql and "sys.database_scoped_configurations" in sql:
            return [] if preview is None else [(preview,)]
        return []

    return respond


# --- capabilities ------------------------------------------------------------


def test_detect_capabilities_sql_server(fake_conn) -> None:  # type: ignore[no-untyped-def]
    caps = db.detect_capabilities(fake_conn(caps_responder()))
    assert caps.vector_type and caps.fulltext_installed
    assert caps.platform == "SQL Server 2025 (Developer Edition (64-bit))"
    assert caps.vector_index_needs_preview and caps.preview_features is False
    assert not caps.is_azure


def test_detect_capabilities_azure_sql_db(fake_conn) -> None:  # type: ignore[no-untyped-def]
    caps = db.detect_capabilities(fake_conn(caps_responder(engine=5, version="12.0.2000.8")))
    assert caps.platform == "Azure SQL Database"
    assert not caps.vector_index_needs_preview
    assert db.enable_preview_features(fake_conn(), caps) == "not needed on Azure SQL Database"


def test_detect_capabilities_without_vector_type(fake_conn) -> None:  # type: ignore[no-untyped-def]
    caps = db.detect_capabilities(fake_conn(caps_responder(vector_ok=False, version="16.0.1")))
    assert not caps.vector_type
    assert caps.platform.startswith("SQL Server 2022")
    assert "VECTOR" in caps.notes[0]


def test_enable_preview_features_runs_alter(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn()
    caps = db.Capabilities(engine_edition=3, preview_features=False)
    assert db.enable_preview_features(conn, caps).startswith("enabled")
    assert conn.calls[-1][0] == "ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON"
    assert db.enable_preview_features(conn, caps) == "already on"


def test_enable_preview_features_reports_failure(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: PermissionError("no ALTER permission"))
    caps = db.Capabilities(engine_edition=3, preview_features=False)
    assert "could not enable" in db.enable_preview_features(conn, caps)


# --- init --------------------------------------------------------------------


def test_init_refuses_engine_without_vectors(fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(db.UnsupportedError, match="no VECTOR type"):
        db.init_schema(fake_conn(caps_responder(vector_ok=False)), 1536)


def test_init_refuses_dims_change(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def extra(sql: str, params: tuple[Any, ...]) -> Any:
        if "OBJECT_ID(N'dbo.sqlvec_meta', N'U')" in sql:
            return [(123,)]
        if "SELECT meta_key, meta_value" in sql:
            return [("dims", "384")]
        return None

    with pytest.raises(db.SchemaError, match=r"VECTOR\(384\)"):
        db.init_schema(fake_conn(caps_responder(extra=extra)), 1536)


def test_init_without_fulltext_skips_hybrid(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(caps_responder(fulltext=0))
    report = db.init_schema(conn, 64)
    assert report.applied == ["001_schema.sql", "002_fulltext.sql", "003_exact_search.sql"]
    assert not report.hybrid_procs
    assert any("full-text" in n for n in report.notices)
    assert any("VECTOR(64)" in sql for sql, _ in conn.calls)
    # settings are stored with parameters, not string-built
    meta_writes = conn.sql_matching("UPDATE dbo.sqlvec_meta")
    assert ("64", "dims", "dims", "64") in [p for _, p in meta_writes]


def test_init_with_fulltext_applies_hybrid(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def extra(sql: str, params: tuple[Any, ...]) -> Any:
        if "FROM sys.fulltext_indexes" in sql and "COUNT(*)" in sql:
            return [(1,)]
        return None

    report = db.init_schema(fake_conn(caps_responder(extra=extra)), 64)
    assert report.hybrid_procs and "004_hybrid_search.sql" in report.applied


# --- searches are parameterized -----------------------------------------------


def test_search_exact_binds_everything(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: [(7, "a.md", 0, "text", 0.25)])
    res = db.search_exact(conn, [1.0, 0.0, 0.0], 3, k=4, filters={"team": EVIL})
    sql, params = conn.calls[-1]
    assert sql == "EXEC dbo.sqlvec_search_exact @query_vector = ?, @k = ?, @filter = ?"
    assert params[0] == "[1,0,0]" and params[1] == 4
    assert json.loads(params[2]) == {"team": EVIL}
    assert EVIL not in sql
    assert res[0].distance == 0.25 and res[0].score == pytest.approx(0.75)


def test_search_exact_checks_dims_and_k(fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        db.search_exact(fake_conn(), [1.0, 0.0], 3)
    with pytest.raises(ConfigError):
        db.search_exact(fake_conn(), [1.0, 0.0, 0.0], 3, k=0)


def _hybrid_ready(rows: list[tuple[Any, ...]]) -> Callable[[str, tuple[Any, ...]], Any]:
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "OBJECT_ID(N'dbo.sqlvec_search_hybrid', N'P')" in sql:
            return [(999,)]
        if sql.startswith("EXEC dbo.sqlvec_search_"):
            return rows
        return []

    return respond


def test_search_hybrid_binds_query_text(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(
        _hybrid_ready([(1, "a.md", 0, "t", 0.0325, 1, 2), (2, "b.md", 1, "u", 0.016, None, 1)])
    )
    res = db.search_hybrid(conn, EVIL, [0.5, 0.5], 2, k=2)
    sql, params = conn.calls[-1]
    assert sql.startswith("EXEC dbo.sqlvec_search_hybrid @query_text = ?, @query_vector = ?")
    assert params[0] == EVIL and EVIL not in sql
    assert params[3] is None  # no filter -> NULL
    assert params[4:] == (50, 60)
    assert res[0].vector_rank == 1 and res[0].fts_rank == 2
    assert res[1].vector_rank is None


def test_search_fts_numbers_ranks(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(_hybrid_ready([(3, "a.md", 0, "t", 120.0), (4, "b.md", 0, "u", 80.0)]))
    res = db.search_fts(conn, "error 1205 deadlock", k=2)
    assert [r.fts_rank for r in res] == [1, 2]
    assert conn.calls[-1][1][0] == "error 1205 deadlock"


def test_hybrid_and_fts_need_fulltext(fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(db.UnsupportedError, match="full-text"):
        db.search_hybrid(fake_conn(), "q", [1.0], 1)
    with pytest.raises(db.UnsupportedError):
        db.search_fts(fake_conn(), "q")


def test_queries_without_words_are_rejected(fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="no words"):
        db.search_fts(fake_conn(_hybrid_ready([])), "  ?! ")


def test_filter_json_is_sorted_and_stringified() -> None:
    assert db.filter_json(None) is None and db.filter_json({}) is None
    assert db.filter_json({"b": "2", "a": "1"}) == '{"a": "1", "b": "2"}'


# --- ANN ---------------------------------------------------------------------


def test_ann_falls_back_to_legacy_syntax(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "WITH APPROXIMATE" in sql:
            return RuntimeError("Incorrect syntax near 'APPROXIMATE'")
        if "TOP_N" in sql:
            return [(5, 0.1), (9, 0.2)]
        return []

    conn = fake_conn(respond)
    hits, legacy = db.ann_ids(conn, db.CHUNKS_TABLE, [1.0, 0.0], 2, 2)
    assert legacy and hits == [(5, 0.1), (9, 0.2)]
    # the cache remembers: next call goes straight to the legacy form
    conn.calls.clear()
    db.ann_ids(conn, db.CHUNKS_TABLE, [1.0, 0.0], 2, 2)
    assert "TOP_N" in conn.calls[0][0]


def test_ann_reports_both_errors(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: RuntimeError("feature is in preview"))
    with pytest.raises(db.UnsupportedError, match=r"WITH APPROXIMATE syntax.*TOP_N syntax"):
        db.ann_ids(conn, db.CHUNKS_TABLE, [1.0], 1, 1)


def test_ann_filter_only_on_chunks_and_bound(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: [(1, 0.0)])
    db.ann_ids(conn, db.CHUNKS_TABLE, [1.0], 1, 3, filters={"k": EVIL}, legacy=False)
    sql, params = conn.calls[-1]
    assert "sqlvec_matches_filter" in sql and EVIL not in sql
    assert json.loads(params[1]) == {"k": EVIL}
    db.ann_ids(conn, "dbo.sqlvec_bench_items", [1.0], 1, 3, filters={"k": "v"}, legacy=False)
    sql, params = conn.calls[-1]
    assert "sqlvec_matches_filter" not in sql and params[1] is None


def test_search_ann_requires_index(fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(db.UnsupportedError, match="sqlvec index"):
        db.search_ann(fake_conn(), [1.0], 1)


def test_search_ann_joins_rows_in_distance_order(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "FROM sys.vector_indexes" in sql:
            return [("vix", "cosine", "DiskANN", "3")]
        if "VECTOR_SEARCH" in sql:
            return [(2, 0.1), (1, 0.3)]
        if "OPENJSON(?)" in sql:
            return [(1, "a.md", 0, "one"), (2, "b.md", 0, "two")]
        return []

    res = db.search_ann(fake_conn(respond), [1.0, 0.0], 2, k=2)
    assert [r.chunk_id for r in res] == [2, 1]
    assert res[0].score == pytest.approx(0.9)


def test_create_vector_index_needs_100_rows(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "COUNT_BIG(*)" in sql:
            return [(42,)]
        return []

    msg = db.create_vector_index(fake_conn(respond), db.Capabilities(engine_edition=5))
    assert msg.startswith("skipped") and "100" in msg and "42" in msg


def test_create_vector_index_reports_refusal(fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "COUNT_BIG(*)" in sql:
            return [(500,)]
        if "CREATE VECTOR INDEX" in sql:
            return RuntimeError("CREATE VECTOR INDEX is not supported")
        return []

    caps = db.Capabilities(engine_edition=3, preview_features=True, edition="Developer")
    msg = db.create_vector_index(fake_conn(respond), caps)
    assert msg.startswith("skipped") and "refused" in msg


def test_create_vector_index_warns_on_old_version(fake_conn) -> None:  # type: ignore[no-untyped-def]
    state = {"created": False}

    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "COUNT_BIG(*)" in sql:
            return [(500,)]
        if "CREATE VECTOR INDEX" in sql:
            state["created"] = True
            return []
        if "FROM sys.vector_indexes" in sql and "TOP (1)" in sql:
            return [("vix", "cosine", "DiskANN", "2")] if state["created"] else []
        return []

    msg = db.create_vector_index(fake_conn(respond), db.Capabilities(engine_edition=5))
    assert msg.startswith("created") and "read-only" in msg


# --- create database ---------------------------------------------------------


def test_create_database_rejects_unsafe_names() -> None:
    with pytest.raises(ConfigError):
        db.create_database("Server=x;Database=a];DROP DATABASE b;--")
    with pytest.raises(ConfigError, match="no Database"):
        db.create_database("Server=x")


def test_create_database_uses_master(monkeypatch: pytest.MonkeyPatch, fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: [(None,)] if "DB_ID" in s else [])
    seen: list[str] = []

    def fake_connect(conn_str: str, autocommit: bool = True, timeout: int = 15) -> Any:
        seen.append(conn_str)
        return conn

    monkeypatch.setattr(db, "connect", fake_connect)
    assert db.create_database("Server=x;Database=sqlvec_test;UID=sa") is True
    assert "Database=master" in seen[0]
    assert conn.calls[0] == ("SELECT DB_ID(?)", ("sqlvec_test",))
    assert conn.calls[1][0] == "CREATE DATABASE [sqlvec_test]"


def test_wait_for_fulltext_polls_until_idle(fake_conn) -> None:  # type: ignore[no-untyped-def]
    states = iter([[(3, 1)], [(1, 0)], [(0, 0)]])
    conn = fake_conn(lambda s, p: next(states))
    sleeps: list[float] = []
    assert db.wait_for_fulltext(conn, timeout=10, sleep=sleeps.append) is True
    assert len(sleeps) == 2
