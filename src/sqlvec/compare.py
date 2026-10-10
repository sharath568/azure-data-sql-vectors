"""SQL Server 2025 / Azure SQL next to PostgreSQL + pgvector, operation by operation.

This is the "if you know one, here's the other" table behind ``sqlvec
compare-pg`` and ``docs/compare-pgvector.md``. The T-SQL column is what this
repo actually runs. The pgvector column is the equivalent from the sibling
project (azure-data-pg-rag). Notes call out where they really differ.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    """One operation, both dialects."""

    operation: str
    sql_server: str
    pgvector: str
    note: str = ""


ROWS: tuple[Row, ...] = (
    Row(
        "Enable vectors",
        "Nothing to install: VECTOR is built in (SQL Server 2025, Azure SQL DB, Azure SQL MI)",
        "CREATE EXTENSION vector;",
        "On Azure Flexible Server the extension must be allow-listed first.",
    ),
    Row(
        "Column type",
        "embedding VECTOR(1536) NOT NULL",
        "embedding vector(1536) NOT NULL",
        "float32 both. SQL Server: up to 1,998 dims (3,996 as float16, preview on 2025). "
        "pgvector: up to 16,000 stored, 2,000 indexable (4,000 with halfvec).",
    ),
    Row(
        "Half precision",
        "VECTOR(3072, float16)",
        "halfvec(3072)",
        "float16 is GA in Azure SQL, preview (PREVIEW_FEATURES) in SQL Server 2025.",
    ),
    Row(
        "Insert a vector",
        "INSERT ... VALUES (CAST(? AS VECTOR(1536)))  -- ? = '[0.1,0.2,...]'",
        "INSERT ... VALUES (%s::vector)  -- or a numpy array via the pgvector adapter",
        "Python drivers send SQL Server vectors as JSON text.",
    ),
    Row(
        "Cosine distance",
        "VECTOR_DISTANCE('cosine', embedding, @qv)",
        "embedding <=> :qv",
        "Both return 1 - cosine similarity (0..2).",
    ),
    Row(
        "Euclidean (L2) distance",
        "VECTOR_DISTANCE('euclidean', embedding, @qv)",
        "embedding <-> :qv",
    ),
    Row(
        "Dot product",
        "VECTOR_DISTANCE('dot', embedding, @qv)",
        "embedding <#> :qv",
        "Both return the NEGATIVE dot product, so smaller is closer.",
    ),
    Row(
        "Norm / normalize",
        "VECTOR_NORM(v, 'norm2') / VECTOR_NORMALIZE(v, 'norm2')",
        "vector_norm(v) / l2_normalize(v)",
        "SQL Server also has 'norm1' and 'norminf'.",
    ),
    Row(
        "Exact top-k",
        "SELECT TOP (10) id, VECTOR_DISTANCE('cosine', embedding, @qv) AS d FROM chunks ORDER BY d",
        "SELECT id, embedding <=> :qv AS d FROM chunks ORDER BY d LIMIT 10",
        "VECTOR_DISTANCE never uses an index; in Postgres the same ORDER BY uses one if present.",
    ),
    Row(
        "ANN index",
        "CREATE VECTOR INDEX vix ON chunks(embedding) WITH (METRIC = 'cosine', TYPE = 'DiskANN')",
        "CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops)",
        "DiskANN only on SQL Server (needs >= 100 rows and an INT clustered PK). "
        "GA in Azure SQL DB, preview in SQL Server 2025. "
        "Postgres: HNSW or IVFFlat; DiskANN via pg_diskann on Azure.",
    ),
    Row(
        "ANN query",
        "SELECT TOP (10) WITH APPROXIMATE t.id, r.distance FROM VECTOR_SEARCH(TABLE = chunks AS t, "
        "COLUMN = embedding, SIMILAR_TO = @qv, METRIC = 'cosine') AS r ORDER BY r.distance",
        "SELECT id FROM chunks ORDER BY embedding <=> :qv LIMIT 10  -- same SQL as exact",
        "SQL Server makes approximate explicit. Older DiskANN builds use TOP_N = k instead.",
    ),
    Row(
        "ANN tuning knob",
        "None exposed (the optimizer decides between DiskANN and kNN)",
        "SET hnsw.ef_search = 100;",
        "",
    ),
    Row(
        "Filtered ANN",
        "WHERE clause applied during the search (latest index version); "
        "post-filter on older versions",
        "SET hnsw.iterative_scan = relaxed_order; (pgvector >= 0.8)",
        "Both used to post-filter and could return fewer than k rows.",
    ),
    Row(
        "Writes after ANN index",
        "Full DML on the latest index version; read-only table on the earlier one",
        "Normal INSERT/UPDATE/DELETE",
        "Check sys.vector_indexes build_parameters $.Version (3 = latest).",
    ),
    Row(
        "Keyword search",
        "FREETEXTTABLE(chunks, content, @q) / CONTAINSTABLE(...)",
        "to_tsvector('english', content) @@ plainto_tsquery(:q), ts_rank_cd(...)",
        "SQL Server full-text is a separate component on Linux (mssql-server-fts); "
        "built in on Azure SQL.",
    ),
    Row(
        "Hybrid (RRF)",
        "EXEC dbo.sqlvec_search_hybrid @query_text, @query_vector, @k",
        "SELECT * FROM pgrag_hybrid_search(:q, :qv, :k)",
        "Same RRF maths (k = 60) in a stored procedure vs a SQL function.",
    ),
    Row(
        "JSON metadata filter",
        "OPENJSON(metadata) / JSON_VALUE(metadata, '$.team') = 'platform'",
        'metadata @> \'{"team": "platform"}\'::jsonb  (GIN index)',
        "Postgres can index the containment test; here it's a per-row check.",
    ),
    Row(
        "In-database embeddings",
        "CREATE EXTERNAL MODEL m ...; AI_GENERATE_EMBEDDINGS(@text USE MODEL m)",
        "azure_openai.create_embeddings('deployment', :text)  -- azure_ai extension",
        "GA on Azure SQL and SQL Server 2025 since May 2026; azure_ai is Azure-only.",
    ),
    Row(
        "In-database chunking",
        "AI_GENERATE_CHUNKS(SOURCE = @text, CHUNK_TYPE = FIXED, CHUNK_SIZE = 1000)",
        "No built-in; chunk in the app (or azure_ai / a PL/pgSQL function)",
        "",
    ),
    Row(
        "Free place to try it",
        "Azure SQL DB free offer (100k vCore-seconds/month) or SQL Server 2025 Developer in Docker",
        "Flexible Server B1ms (12 months free) or pgvector in Docker",
        "",
    ),
)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def to_markdown(rows: tuple[Row, ...] = ROWS, notes: bool = True) -> str:
    """Markdown table (operations as rows)."""
    if notes:
        lines = [
            "| operation | SQL Server 2025 / Azure SQL | PostgreSQL + pgvector | notes |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {_cell(r.operation)} | `{_cell(r.sql_server)}` | `{_cell(r.pgvector)}` | "
            f"{_cell(r.note)} |"
            for r in rows
        ]
    else:
        lines = [
            "| operation | SQL Server 2025 / Azure SQL | PostgreSQL + pgvector |",
            "|---|---|---|",
        ]
        lines += [
            f"| {_cell(r.operation)} | `{_cell(r.sql_server)}` | `{_cell(r.pgvector)}` |"
            for r in rows
        ]
    return "\n".join(lines) + "\n"


def to_text(rows: tuple[Row, ...] = ROWS) -> str:
    """Plain text, one block per operation (readable in a narrow terminal)."""
    blocks = []
    for r in rows:
        block = [r.operation, f"  SQL Server : {r.sql_server}", f"  pgvector   : {r.pgvector}"]
        if r.note:
            block.append(f"  note       : {r.note}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n"
