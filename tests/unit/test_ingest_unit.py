from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sqlvec import ingest
from sqlvec.config import ConfigError
from sqlvec.embeddings import HashEmbedder


def test_insert_chunk_sql_casts_bound_vector() -> None:
    sql = ingest.insert_chunk_sql(384)
    assert sql.endswith("CAST(? AS VECTOR(384)))")
    assert sql.count("?") == 5
    with pytest.raises(ConfigError):
        ingest.insert_chunk_sql(5000)


def test_chunk_rows_shape() -> None:
    rows = ingest.chunk_rows(7, ["a", "b"], [[1.0, 0.0], [0.0, 1.0]], {"team": "x"}, 2)
    assert rows[0] == (7, 0, "a", '{"team": "x"}', "[1,0]")
    assert rows[1][1] == 1
    with pytest.raises(ValueError):
        ingest.chunk_rows(7, ["a"], [], {}, 2)


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text("# Alpha\n\n" + "Vectors live next to rows. " * 20, "utf-8")
    (tmp_path / "b.txt").write_text("Full-text finds error 1205 exactly.", "utf-8")
    (tmp_path / "empty.md").write_text("   \n", "utf-8")
    (tmp_path / "skip.pdf").write_bytes(b"%PDF")
    return tmp_path


def test_ingest_path_writes_in_transactions(tmp_path: Path, fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if sql.startswith("INSERT INTO dbo.sqlvec_documents"):
            return [(11,)]
        return []

    conn = fake_conn(respond)
    report = ingest.ingest_path(
        conn,
        _corpus(tmp_path),
        HashEmbedder(16),
        dims=16,
        chunk_size=200,
        overlap=20,
        extra_metadata={"team": "docs"},
    )
    assert report.files_seen == 3 and report.files_ingested == 2 and report.files_empty == 1
    assert conn.commits == 2 and conn.autocommit is True
    sql, rows = conn.many[0]
    assert "CAST(? AS VECTOR(16))" in sql
    assert all(r[0] == 11 for r in rows)
    meta = json.loads(rows[0][3])
    assert meta == {"ext": "md", "team": "docs"}
    assert json.loads(rows[0][4]) and len(json.loads(rows[0][4])) == 16
    # document text only ever travels as a parameter
    assert all("Vectors live next to rows" not in s for s, _ in conn.calls)


def test_ingest_skips_unchanged(tmp_path: Path, fake_conn) -> None:  # type: ignore[no-untyped-def]
    from sqlvec.chunking import iter_documents

    root = _corpus(tmp_path)
    hashes = {d.source: d.content_hash for d in iter_documents(root)}

    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if sql == ingest.GET_HASH_SQL:
            return [(hashes[params[0]],)]
        return []

    conn = fake_conn(respond)
    report = ingest.ingest_path(conn, root, HashEmbedder(16), dims=16)
    assert report.files_unchanged == 3 and report.files_ingested == 0 and not conn.many


def test_ingest_rolls_back_and_explains_read_only(tmp_path: Path, fake_conn) -> None:  # type: ignore[no-untyped-def]
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if sql.startswith("DELETE FROM dbo.sqlvec_documents"):
            return RuntimeError("Cannot modify table with a vector index; table is read-only")
        return []

    conn = fake_conn(respond)
    with pytest.raises(RuntimeError, match="sqlvec index --drop"):
        ingest.ingest_path(conn, _corpus(tmp_path), HashEmbedder(16), dims=16)
    assert conn.rollbacks == 1 and conn.autocommit is True


def test_ingest_rejects_dims_mismatch(tmp_path: Path, fake_conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="16-dim"):
        ingest.ingest_path(fake_conn(), tmp_path, HashEmbedder(16), dims=32)


def test_prune_needs_folder(tmp_path: Path, fake_conn) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "x.md"
    f.write_text("hello", "utf-8")
    with pytest.raises(ValueError, match="prune"):
        ingest.ingest_path(fake_conn(), f, HashEmbedder(16), dims=16, prune=True)


def test_delete_missing_binds_sources(fake_conn) -> None:  # type: ignore[no-untyped-def]
    conn = fake_conn(lambda s, p: [(2,)])
    assert ingest.delete_missing(conn, ["a.md", "it's.md"]) == 2
    sql, params = conn.calls[-1]
    assert "OPENJSON(?)" in sql and json.loads(params[0]) == ["a.md", "it's.md"]
