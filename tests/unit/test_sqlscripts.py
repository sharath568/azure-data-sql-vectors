"""The .sql files: rendering, GO splitting, and the "no user input in SQL text" rule."""

from __future__ import annotations

import re

import pytest

from sqlvec import sqlscripts
from sqlvec.config import ConfigError


def test_all_scripts_exist_and_render() -> None:
    names = [
        *sqlscripts.SCHEMA_SCRIPTS,
        sqlscripts.HYBRID_SCRIPT,
        sqlscripts.VECTOR_INDEX_SCRIPT,
    ]
    for name in names:
        batches = sqlscripts.script_batches(name, 1536)
        assert batches, name
        for b in batches:
            assert "__DIMS__" not in b
            assert not re.search(r"^\s*GO\s*$", b, re.MULTILINE)


def test_schema_uses_dims_and_int_clustered_pk() -> None:
    text = "\n".join(b for _, b in sqlscripts.schema_batches(384))
    assert "VECTOR(384)" in text
    assert "PRIMARY KEY CLUSTERED" in text  # DiskANN requires an INT clustered PK
    assert "id          INT IDENTITY" in text


def test_hybrid_proc_is_rrf_with_float_maths() -> None:
    text = "\n".join(sqlscripts.script_batches(sqlscripts.HYBRID_SCRIPT, 1536))
    assert "FREETEXTTABLE" in text
    assert "@rrf_k        INT = 60" in text
    assert "CAST(1 AS FLOAT) / (@rrf_k + v.rnk)" in text
    assert "FULL OUTER JOIN" in text


def test_vector_index_script_is_diskann_cosine() -> None:
    text = sqlscripts.read_script(sqlscripts.VECTOR_INDEX_SCRIPT)
    assert "TYPE = 'DiskANN'" in text and "METRIC = 'cosine'" in text


def test_split_batches_like_sqlcmd() -> None:
    script = "SELECT 1\nGO\n-- only a comment\nGO\n  go  \nSELECT 2\nGo -- trailing comment\n"
    assert sqlscripts.split_batches(script) == ["SELECT 1", "SELECT 2"]


def test_split_does_not_split_on_go_inside_a_line() -> None:
    assert sqlscripts.split_batches("SELECT 'GO' AS x\nGO") == ["SELECT 'GO' AS x"]


@pytest.mark.parametrize("dims", [0, 1999, -5])
def test_render_rejects_bad_dims(dims: int) -> None:
    with pytest.raises(ConfigError):
        sqlscripts.render("VECTOR(__DIMS__)", dims=dims)


def test_render_rejects_non_int_text() -> None:
    with pytest.raises(ConfigError):
        sqlscripts.render("TOP (__K__)", k="10; DROP TABLE x")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        sqlscripts.render("TOP (__K__)", k=True)  # type: ignore[arg-type]


def test_render_only_allows_own_tables() -> None:
    with pytest.raises(ConfigError):
        sqlscripts.render("FROM __TABLE__", table="dbo.users; DROP TABLE x")
    assert (
        sqlscripts.render("FROM __TABLE__", table="dbo.sqlvec_chunks") == "FROM dbo.sqlvec_chunks"
    )


def test_render_fails_on_leftover_placeholder() -> None:
    with pytest.raises(ConfigError, match="__K__"):
        sqlscripts.render("SELECT TOP (__K__) 1", dims=3)


def test_render_ignores_placeholders_in_comments() -> None:
    assert sqlscripts.render("-- mentions __DIMS__\nSELECT 1") == "-- mentions __DIMS__\nSELECT 1"


def test_ann_query_new_syntax() -> None:
    sql = sqlscripts.ann_query("dbo.sqlvec_chunks", 1536, 10, legacy=False, use_filter=True)
    assert "SELECT TOP (10) WITH APPROXIMATE" in sql
    assert "TOP_N" not in sql
    assert "METRIC = 'cosine'" in sql
    assert "dbo.sqlvec_matches_filter(t.metadata, @filter)" in sql
    assert sql.count("?") == 2  # vector and filter, both bound
    assert "--" not in sql  # comments stripped so a '?' in them can't confuse the driver


def test_ann_query_legacy_syntax_oversamples() -> None:
    sql = sqlscripts.ann_query(
        "dbo.sqlvec_bench_items", 384, 10, legacy=True, use_filter=False, top_n=40
    )
    assert "TOP_N = 40" in sql
    assert "WITH APPROXIMATE" not in sql
    assert "sqlvec_matches_filter" not in sql
    assert sql.count("?") == 2


def test_legacy_top_n_never_below_k() -> None:
    sql = sqlscripts.ann_query("dbo.sqlvec_chunks", 8, 25, legacy=True, use_filter=False, top_n=5)
    assert "TOP_N = 25" in sql


def test_exact_bench_query() -> None:
    sql = sqlscripts.exact_bench_query("dbo.sqlvec_bench_items", 384, 10)
    assert "VECTOR_DISTANCE('cosine', t.embedding, @qv)" in sql
    assert "CAST(? AS VECTOR(384))" in sql
    assert sql.count("?") == 1


def test_sql_dir_found() -> None:
    assert (sqlscripts.sql_dir() / "001_schema.sql").is_file()
