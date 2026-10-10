"""Database access: connect, detect capabilities, create schema, search.

Uses Microsoft's ``mssql-python`` driver through the plain DB-API surface
(``cursor.execute(sql, params)`` with ``?`` placeholders), so swapping in
``pyodbc`` would only touch :func:`connect`.

Every value that comes from a user (query text, vectors, filters, k, file
contents) is a bind parameter. See :mod:`sqlvec.sqlscripts` for the short
list of things that are written into SQL text, and how they are validated.
"""

from __future__ import annotations

import contextlib
import json
import time
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlvec import sqlscripts, vecjson
from sqlvec.config import (
    ConfigError,
    database_name,
    validate_dims,
    validate_identifier,
    with_database,
)

CHUNKS_TABLE = "dbo.sqlvec_chunks"
VECTOR_INDEX_NAME = "vix_sqlvec_chunks_embedding"
MIN_ROWS_FOR_VECTOR_INDEX = 100  # CREATE VECTOR INDEX fails below this (Msg 42266)

# SERVERPROPERTY('EngineEdition') values that matter here.
ENGINE_AZURE_SQL_DB = 5
ENGINE_AZURE_SQL_MI = 8


class SchemaError(RuntimeError):
    """Raised when the existing schema doesn't match the settings."""


class UnsupportedError(RuntimeError):
    """Raised when the connected engine can't do what was asked."""


# --------------------------------------------------------------------------
# connections


def connect(conn_str: str, autocommit: bool = True, timeout: int = 15) -> Any:
    """Open a connection with ``mssql-python``."""
    import mssql_python

    return mssql_python.connect(conn_str, autocommit=autocommit, timeout=timeout)


@contextmanager
def connection(conn_str: str, autocommit: bool = True) -> Iterator[Any]:
    """Context manager that always closes the connection."""
    conn = connect(conn_str, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    """Run one statement or batch and return the cursor."""
    cur = conn.cursor()
    if params:
        cur.execute(sql, tuple(params))
    else:
        cur.execute(sql)
    return cur


def fetchall(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    """Run a query and return all rows as tuples."""
    cur = execute(conn, sql, params)
    try:
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def fetchone(conn: Any, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
    """Run a query and return the first row (or None)."""
    cur = execute(conn, sql, params)
    try:
        row = cur.fetchone()
        return tuple(row) if row is not None else None
    finally:
        cur.close()


def run(conn: Any, sql: str, params: Sequence[Any] = ()) -> None:
    """Run a statement and discard any result."""
    execute(conn, sql, params).close()


def create_database(conn_str: str) -> bool:
    """Create the database named in ``conn_str`` if it doesn't exist.

    Connects to ``master`` for this. The name is validated as a plain
    identifier and bracket-quoted, since CREATE DATABASE can't take a
    parameter. Returns True if the database was created.
    """
    name = database_name(conn_str)
    if not name:
        raise ConfigError("the connection string has no Database= to create")
    validate_identifier(name, "database name")
    master = with_database(conn_str, "master")
    with connection(master) as conn:
        exists = fetchone(conn, "SELECT DB_ID(?)", (name,))
        if exists and exists[0] is not None:
            return False
        run(conn, f"CREATE DATABASE [{name}]")
    return True


# --------------------------------------------------------------------------
# capabilities


@dataclass
class Capabilities:
    """What the connected engine can do, discovered by asking it."""

    product_version: str = ""
    engine_edition: int = 0
    edition: str = ""
    version_banner: str = ""
    vector_type: bool = False
    fulltext_installed: bool = False
    preview_features: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def platform(self) -> str:
        """Human label for the engine."""
        if self.engine_edition == ENGINE_AZURE_SQL_DB:
            return "Azure SQL Database"
        if self.engine_edition == ENGINE_AZURE_SQL_MI:
            return "Azure SQL Managed Instance"
        major = self.product_version.split(".", 1)[0]
        label = {"17": "SQL Server 2025", "16": "SQL Server 2022"}.get(major, "SQL Server")
        return f"{label} ({self.edition})" if self.edition else label

    @property
    def is_azure(self) -> bool:
        return self.engine_edition in (ENGINE_AZURE_SQL_DB, ENGINE_AZURE_SQL_MI)

    @property
    def vector_index_needs_preview(self) -> bool:
        """DiskANN / VECTOR_SEARCH are preview on boxed SQL Server 2025 (and MI on
        the SQL Server 2025 update policy, which we can't tell apart from here)."""
        return self.engine_edition != ENGINE_AZURE_SQL_DB

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["platform"] = self.platform
        return out


def detect_capabilities(conn: Any) -> Capabilities:
    """Ask the server what it supports. Never raises for a missing feature."""
    caps = Capabilities()
    row = fetchone(
        conn,
        "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)), "
        "CAST(SERVERPROPERTY('EngineEdition') AS INT), "
        "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)), "
        "CAST(@@VERSION AS NVARCHAR(4000)), "
        "CAST(FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') AS INT)",
    )
    if row:
        caps.product_version = str(row[0] or "")
        caps.engine_edition = int(row[1] or 0)
        caps.edition = str(row[2] or "")
        caps.version_banner = " ".join(str(row[3] or "").split())
        caps.fulltext_installed = bool(row[4])
    try:
        fetchone(
            conn,
            "SELECT VECTOR_DISTANCE('cosine', CAST('[1,0]' AS VECTOR(2)), "
            "CAST('[0,1]' AS VECTOR(2)))",
        )
        caps.vector_type = True
    except Exception as exc:
        caps.notes.append(f"vector type not available: {_short(exc)}")
    try:
        pf = fetchone(
            conn,
            "SELECT CAST(value AS INT) FROM sys.database_scoped_configurations "
            "WHERE name = 'PREVIEW_FEATURES'",
        )
        caps.preview_features = None if pf is None else bool(pf[0])
    except Exception:
        caps.preview_features = None
    return caps


def enable_preview_features(conn: Any, caps: Capabilities) -> str:
    """Turn on PREVIEW_FEATURES where DiskANN needs it. Returns what happened."""
    if not caps.vector_index_needs_preview:
        return "not needed on Azure SQL Database"
    if caps.preview_features:
        return "already on"
    try:
        run(conn, "ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON")
    except Exception as exc:
        return f"could not enable PREVIEW_FEATURES: {_short(exc)}"
    caps.preview_features = True
    return "enabled PREVIEW_FEATURES for this database"


def _short(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# schema


@dataclass
class InitReport:
    """What ``init_schema`` did."""

    capabilities: Capabilities
    applied: list[str] = field(default_factory=list)
    fulltext_index: bool = False
    hybrid_procs: bool = False
    vector_index: str = "not requested"
    notices: list[str] = field(default_factory=list)


def get_meta(conn: Any) -> dict[str, str]:
    """Contents of ``dbo.sqlvec_meta`` (empty if the table doesn't exist)."""
    exists = fetchone(conn, "SELECT OBJECT_ID(N'dbo.sqlvec_meta', N'U')")
    if not exists or exists[0] is None:
        return {}
    return {
        str(k): str(v)
        for k, v in fetchall(conn, "SELECT meta_key, meta_value FROM dbo.sqlvec_meta")
    }


def set_meta(conn: Any, key: str, value: str) -> None:
    """Upsert one meta value."""
    run(
        conn,
        "UPDATE dbo.sqlvec_meta SET meta_value = ? WHERE meta_key = ?; "
        "IF @@ROWCOUNT = 0 INSERT INTO dbo.sqlvec_meta (meta_key, meta_value) VALUES (?, ?);",
        (value, key, key, value),
    )


def fulltext_index_exists(conn: Any) -> bool:
    row = fetchone(
        conn,
        "SELECT COUNT(*) FROM sys.fulltext_indexes "
        "WHERE object_id = OBJECT_ID(N'dbo.sqlvec_chunks')",
    )
    return bool(row and row[0])


def hybrid_procs_exist(conn: Any) -> bool:
    row = fetchone(conn, "SELECT OBJECT_ID(N'dbo.sqlvec_search_hybrid', N'P')")
    return bool(row and row[0] is not None)


def init_schema(
    conn: Any,
    dims: int,
    *,
    embedder_name: str = "hash",
    vector_index: bool = False,
) -> InitReport:
    """Create tables, full-text index (if available) and procedures. Idempotent."""
    validate_dims(dims)
    caps = detect_capabilities(conn)
    report = InitReport(capabilities=caps)
    if not caps.vector_type:
        raise UnsupportedError(
            f"{caps.platform} {caps.product_version} has no VECTOR type. sqlvec needs "
            "SQL Server 2025, Azure SQL Database or Azure SQL Managed Instance "
            "(SQL Server 2025 / Always-up-to-date update policy)."
        )

    meta = get_meta(conn)
    if meta.get("dims") and int(meta["dims"]) != dims:
        raise SchemaError(
            f"the existing schema uses VECTOR({meta['dims']}) but SQLVEC_EMBED_DIMS is {dims}. "
            "Use the same embedding size, or drop the sqlvec tables first "
            "(sqlvec drop --yes) and re-ingest."
        )

    for name, batch in sqlscripts.schema_batches(dims):
        run(conn, batch)
        if name not in report.applied:
            report.applied.append(name)
    set_meta(conn, "dims", str(dims))
    set_meta(conn, "embedder", embedder_name)
    set_meta(conn, "schema_version", "1")

    report.fulltext_index = fulltext_index_exists(conn)
    if report.fulltext_index:
        for batch in sqlscripts.script_batches(sqlscripts.HYBRID_SCRIPT, dims):
            run(conn, batch)
        report.applied.append(sqlscripts.HYBRID_SCRIPT)
        report.hybrid_procs = True
    else:
        report.notices.append(
            "full-text search is not installed on this server, so the fts and hybrid "
            "modes are unavailable (exact and ann still work). On Linux/containers "
            "install mssql-server-fts (see docker/fts/Dockerfile); Azure SQL has it built in."
        )

    if vector_index:
        report.vector_index = create_vector_index(conn, caps)
    return report


def drop_schema(conn: Any) -> None:
    """Drop every sqlvec object. Data is gone afterwards."""
    stmts = [
        "IF EXISTS (SELECT 1 FROM sys.fulltext_indexes "
        "WHERE object_id = OBJECT_ID(N'dbo.sqlvec_chunks')) "
        "EXEC (N'DROP FULLTEXT INDEX ON dbo.sqlvec_chunks')",
        "DROP PROCEDURE IF EXISTS dbo.sqlvec_search_hybrid_text",
        "DROP PROCEDURE IF EXISTS dbo.sqlvec_search_hybrid",
        "DROP PROCEDURE IF EXISTS dbo.sqlvec_search_fts",
        "DROP PROCEDURE IF EXISTS dbo.sqlvec_search_exact",
        "DROP PROCEDURE IF EXISTS dbo.sqlvec_ingest_text",
        "DROP TABLE IF EXISTS dbo.sqlvec_chunks",
        "DROP TABLE IF EXISTS dbo.sqlvec_documents",
        "DROP TABLE IF EXISTS dbo.sqlvec_meta",
        "DROP FUNCTION IF EXISTS dbo.sqlvec_matches_filter",
        "IF EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name = N'sqlvec_ftcat') "
        "EXEC (N'DROP FULLTEXT CATALOG sqlvec_ftcat')",
    ]
    for stmt in stmts:
        run(conn, stmt)


# --------------------------------------------------------------------------
# vector index


@dataclass
class VectorIndexInfo:
    """What sys.vector_indexes says about an index."""

    name: str
    metric: str | None
    index_type: str | None
    version: str | None


def vector_index_info(conn: Any, table: str = CHUNKS_TABLE) -> VectorIndexInfo | None:
    """The vector index on ``table`` (None if none, or if the view doesn't exist)."""
    sqlscripts.check_table(table)
    try:
        row = fetchone(
            conn,
            "SELECT TOP (1) name, distance_metric, vector_index_type, "
            "JSON_VALUE(build_parameters, '$.Version') "
            "FROM sys.vector_indexes WHERE object_id = OBJECT_ID(?)",
            (table,),
        )
    except Exception:
        return None
    if not row:
        return None
    return VectorIndexInfo(
        name=str(row[0]),
        metric=row[1],
        index_type=row[2],
        version=None if row[3] is None else str(row[3]),
    )


def count_vectors(conn: Any, table: str = CHUNKS_TABLE) -> int:
    sqlscripts.check_table(table)
    row = fetchone(conn, f"SELECT COUNT_BIG(*) FROM {table} WHERE embedding IS NOT NULL")
    return int(row[0]) if row else 0


def create_vector_index(conn: Any, caps: Capabilities | None = None) -> str:
    """Create the DiskANN index on chunks if possible. Returns a status line.

    Never raises for "not supported": the caller prints the reason instead.
    """
    caps = caps or detect_capabilities(conn)
    if vector_index_info(conn) is not None:
        return "exists"
    rows = count_vectors(conn)
    if rows < MIN_ROWS_FOR_VECTOR_INDEX:
        return (
            f"skipped: a vector index needs at least {MIN_ROWS_FOR_VECTOR_INDEX} rows and "
            f"there are {rows}. Ingest more, then run `sqlvec index`. "
            "Below ~50k chunks exact search is fine anyway."
        )
    preview = enable_preview_features(conn, caps)
    try:
        for batch in sqlscripts.script_batches(sqlscripts.VECTOR_INDEX_SCRIPT):
            run(conn, batch)
    except Exception as exc:
        return (
            f"skipped: {caps.platform} refused CREATE VECTOR INDEX ({_short(exc)}). "
            f"PREVIEW_FEATURES: {preview}."
        )
    info = vector_index_info(conn)
    version = info.version if info else None
    msg = f"created (DiskANN, cosine, index version {version or 'unknown'}; {preview})"
    if version is not None and version != "3":
        msg += (
            ". NOTE: this is an earlier index version. The table is read-only while the "
            "index exists; drop it (sqlvec index --drop) before ingesting more."
        )
    return msg


def drop_vector_index(conn: Any) -> bool:
    info = vector_index_info(conn)
    if info is None:
        return False
    run(conn, f"DROP INDEX {VECTOR_INDEX_NAME} ON {CHUNKS_TABLE}")
    return True


# --------------------------------------------------------------------------
# search


@dataclass
class SearchResult:
    """One search hit."""

    chunk_id: int
    source: str
    chunk_index: int
    content: str
    score: float
    distance: float | None = None
    vector_rank: int | None = None
    fts_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def filter_json(filters: Mapping[str, str] | None) -> str | None:
    """Metadata filter as a JSON object string, or None for no filter."""
    if not filters:
        return None
    return json.dumps({str(k): str(v) for k, v in filters.items()}, sort_keys=True)


def _check_k(k: int) -> int:
    return sqlscripts.check_int(k, "k", 1, sqlscripts.MAX_K)


def search_exact(
    conn: Any,
    qvec: Sequence[float],
    dims: int,
    k: int = 5,
    filters: Mapping[str, str] | None = None,
) -> list[SearchResult]:
    """Exact cosine search via ``dbo.sqlvec_search_exact``."""
    rows = fetchall(
        conn,
        "EXEC dbo.sqlvec_search_exact @query_vector = ?, @k = ?, @filter = ?",
        (vecjson.to_json(qvec, dims), _check_k(k), filter_json(filters)),
    )
    return [
        SearchResult(
            chunk_id=int(r[0]),
            source=str(r[1]),
            chunk_index=int(r[2]),
            content=str(r[3]),
            score=1.0 - float(r[4]),
            distance=float(r[4]),
        )
        for r in rows
    ]


def search_fts(
    conn: Any, query: str, k: int = 5, filters: Mapping[str, str] | None = None
) -> list[SearchResult]:
    """Keyword search via ``dbo.sqlvec_search_fts`` (FREETEXTTABLE)."""
    _require_hybrid(conn)
    _require_words(query)
    rows = fetchall(
        conn,
        "EXEC dbo.sqlvec_search_fts @query_text = ?, @k = ?, @filter = ?",
        (query, _check_k(k), filter_json(filters)),
    )
    return [
        SearchResult(
            chunk_id=int(r[0]),
            source=str(r[1]),
            chunk_index=int(r[2]),
            content=str(r[3]),
            score=float(r[4]),
            fts_rank=i,
        )
        for i, r in enumerate(rows, start=1)
    ]


def search_hybrid(
    conn: Any,
    query: str,
    qvec: Sequence[float],
    dims: int,
    k: int = 5,
    filters: Mapping[str, str] | None = None,
    candidates: int = 50,
    rrf_k: int = 60,
) -> list[SearchResult]:
    """Vector + full-text fused with RRF via ``dbo.sqlvec_search_hybrid``."""
    _require_hybrid(conn)
    _require_words(query)
    rows = fetchall(
        conn,
        "EXEC dbo.sqlvec_search_hybrid @query_text = ?, @query_vector = ?, @k = ?, "
        "@filter = ?, @candidates = ?, @rrf_k = ?",
        (
            query,
            vecjson.to_json(qvec, dims),
            _check_k(k),
            filter_json(filters),
            sqlscripts.check_int(candidates, "candidates", 1, 10_000),
            sqlscripts.check_int(rrf_k, "rrf_k", 0, 1_000_000),
        ),
    )
    return [
        SearchResult(
            chunk_id=int(r[0]),
            source=str(r[1]),
            chunk_index=int(r[2]),
            content=str(r[3]),
            score=float(r[4]),
            vector_rank=None if r[5] is None else int(r[5]),
            fts_rank=None if r[6] is None else int(r[6]),
        )
        for r in rows
    ]


def _require_hybrid(conn: Any) -> None:
    if not hybrid_procs_exist(conn):
        raise UnsupportedError(
            "fts and hybrid search need full-text search, which isn't set up in this "
            "database (not installed on the server, or `sqlvec init` ran before it was). "
            "Use --mode exact or --mode ann, or see docker/fts/Dockerfile."
        )


def _require_words(query: str) -> None:
    if not any(ch.isalnum() for ch in query):
        raise ValueError("the query has no words to search for")


# ANN ---------------------------------------------------------------------

# connection -> True if it needed the legacy TOP_N syntax. Weak keys, so a
# closed connection's entry goes away with it (and ids are never reused).
_ann_syntax_cache: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()


def _cached_syntax(conn: Any) -> bool | None:
    try:
        return _ann_syntax_cache.get(conn)
    except TypeError:  # connection type without weakref support
        return None


def _remember_syntax(conn: Any, legacy: bool) -> None:
    with contextlib.suppress(TypeError):
        _ann_syntax_cache[conn] = legacy


def ann_ids(
    conn: Any,
    table: str,
    qvec: Sequence[float],
    dims: int,
    k: int,
    *,
    filters: Mapping[str, str] | None = None,
    oversample: int = 4,
    legacy: bool | None = None,
) -> tuple[list[tuple[int, float]], bool]:
    """Approximate top-k ``(id, distance)`` from VECTOR_SEARCH.

    Tries the current ``SELECT TOP (k) WITH APPROXIMATE`` syntax first and falls
    back to the deprecated ``TOP_N`` form. Returns the rows and whether the
    legacy syntax was used.
    """
    use_filter = bool(filters) and table == CHUNKS_TABLE
    params = (vecjson.to_json(qvec, dims), filter_json(filters) if use_filter else None)
    attempts = [legacy] if legacy is not None else [False, True]
    cached = _cached_syntax(conn)
    if legacy is None and cached is not None:
        attempts = [cached, not cached]
    errors: list[str] = []
    for is_legacy in attempts:
        sql = sqlscripts.ann_query(
            table,
            dims,
            k,
            legacy=is_legacy,
            use_filter=use_filter,
            top_n=k * (oversample if use_filter else 1),
        )
        try:
            rows = fetchall(conn, sql, params)
        except Exception as exc:
            errors.append(f"{'TOP_N' if is_legacy else 'WITH APPROXIMATE'} syntax: {_short(exc)}")
            continue
        _remember_syntax(conn, is_legacy)
        return [(int(r[0]), float(r[1])) for r in rows], is_legacy
    raise UnsupportedError("VECTOR_SEARCH failed. " + " | ".join(errors))


def search_ann(
    conn: Any,
    qvec: Sequence[float],
    dims: int,
    k: int = 5,
    filters: Mapping[str, str] | None = None,
) -> list[SearchResult]:
    """Approximate search on chunks with the DiskANN index."""
    if vector_index_info(conn) is None:
        raise UnsupportedError(
            "there is no vector index on dbo.sqlvec_chunks, so --mode ann would silently "
            "fall back to a full scan. Run `sqlvec index` first (needs >= 100 chunks), "
            "or use --mode exact."
        )
    hits, _legacy = ann_ids(conn, CHUNKS_TABLE, qvec, dims, _check_k(k), filters=filters)
    if not hits:
        return []
    ids_json = json.dumps([h[0] for h in hits])
    rows = fetchall(
        conn,
        "SELECT c.id, d.source, c.chunk_index, c.content "
        "FROM dbo.sqlvec_chunks AS c JOIN dbo.sqlvec_documents AS d ON d.id = c.document_id "
        "WHERE c.id IN (SELECT CAST(value AS INT) FROM OPENJSON(?))",
        (ids_json,),
    )
    by_id = {int(r[0]): r for r in rows}
    out = []
    for chunk_id, dist in hits:
        r = by_id.get(chunk_id)
        if r is None:
            continue
        out.append(
            SearchResult(
                chunk_id=chunk_id,
                source=str(r[1]),
                chunk_index=int(r[2]),
                content=str(r[3]),
                score=1.0 - dist,
                distance=dist,
            )
        )
    return out


# --------------------------------------------------------------------------
# misc


def counts(conn: Any) -> dict[str, int]:
    row = fetchone(
        conn,
        "SELECT (SELECT COUNT(*) FROM dbo.sqlvec_documents), "
        "(SELECT COUNT(*) FROM dbo.sqlvec_chunks)",
    )
    return {"documents": int(row[0]) if row else 0, "chunks": int(row[1]) if row else 0}


def wait_for_fulltext(
    conn: Any,
    timeout: float = 60.0,
    poll: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the full-text index has caught up with recent inserts.

    Full-text population is asynchronous. Returns False on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        row = fetchone(
            conn,
            "SELECT CAST(OBJECTPROPERTYEX(OBJECT_ID(N'dbo.sqlvec_chunks'), "
            "'TableFulltextPendingChanges') AS INT), "
            "CAST(FULLTEXTCATALOGPROPERTY('sqlvec_ftcat', 'PopulateStatus') AS INT)",
        )
        pending = row[0] if row else None
        status = row[1] if row else None
        if (pending in (0, None)) and status in (0, None):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(poll)
