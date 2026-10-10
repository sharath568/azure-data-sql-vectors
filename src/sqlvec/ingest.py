"""Ingest a folder of Markdown / text files into SQL Server or Azure SQL."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlvec import db, sqlscripts, vecjson
from sqlvec.chunking import chunk_text, iter_documents
from sqlvec.embeddings import Embedder


@dataclass
class IngestReport:
    """Counts from one ingest run."""

    files_seen: int = 0
    files_ingested: int = 0
    files_unchanged: int = 0
    files_empty: int = 0
    files_deleted: int = 0
    chunks_written: int = 0
    tokens_embedded: int = 0
    sources: list[str] = field(default_factory=list)


@contextmanager
def transaction(conn: Any) -> Iterator[None]:
    """Run a block in one transaction on a connection that is normally autocommit."""
    previous = conn.autocommit
    conn.autocommit = False
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = previous


def insert_chunk_sql(dims: int) -> str:
    """INSERT for one chunk. The vector arrives as JSON text and is cast server side."""
    sqlscripts.check_int(dims, "dims", 1, 1998)
    return (
        "INSERT INTO dbo.sqlvec_chunks (document_id, chunk_index, content, metadata, embedding) "
        f"VALUES (?, ?, ?, ?, CAST(? AS VECTOR({dims})))"
    )


INSERT_DOCUMENT_SQL = (
    "INSERT INTO dbo.sqlvec_documents (source, title, content_hash, metadata, embedder) "
    "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?)"
)
DELETE_DOCUMENT_SQL = "DELETE FROM dbo.sqlvec_documents WHERE source = ?"
GET_HASH_SQL = "SELECT content_hash FROM dbo.sqlvec_documents WHERE source = ?"


def chunk_rows(
    document_id: int,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
    metadata: Mapping[str, str],
    dims: int,
) -> list[tuple[Any, ...]]:
    """Parameter tuples for :func:`insert_chunk_sql`, one per chunk."""
    if len(texts) != len(vectors):
        raise ValueError(f"{len(texts)} chunks but {len(vectors)} vectors")
    meta_json = json.dumps(dict(metadata), sort_keys=True)
    return [
        (document_id, i, text, meta_json, vecjson.to_json(vec, dims))
        for i, (text, vec) in enumerate(zip(texts, vectors, strict=True))
    ]


def upsert_document(
    conn: Any,
    *,
    source: str,
    title: str,
    content_hash: str,
    metadata: Mapping[str, str],
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
    dims: int,
    embedder_name: str,
) -> int:
    """Replace one document and its chunks. Call inside a transaction."""
    db.run(conn, DELETE_DOCUMENT_SQL, (source,))  # chunks go with it (ON DELETE CASCADE)
    row = db.fetchone(
        conn,
        INSERT_DOCUMENT_SQL,
        (
            source,
            title[:400],
            content_hash,
            json.dumps(dict(metadata), sort_keys=True),
            embedder_name,
        ),
    )
    if not row:
        raise RuntimeError(f"insert of document {source!r} returned no id")
    doc_id = int(row[0])
    rows = chunk_rows(doc_id, texts, vectors, metadata, dims)
    cur = conn.cursor()
    try:
        cur.executemany(insert_chunk_sql(dims), rows)
    finally:
        cur.close()
    return doc_id


def delete_missing(conn: Any, keep_sources: Sequence[str]) -> int:
    """Delete documents whose source is not in ``keep_sources``. Returns how many."""
    keep = json.dumps(list(keep_sources))
    row = db.fetchone(
        conn,
        # NOCOUNT so the first thing the driver sees is the SELECT's result set.
        "SET NOCOUNT ON; DELETE FROM dbo.sqlvec_documents "
        "WHERE source NOT IN (SELECT value FROM OPENJSON(?)); SELECT @@ROWCOUNT;",
        (keep,),
    )
    return int(row[0]) if row else 0


def _friendly(exc: Exception) -> Exception:
    text = str(exc).lower()
    if "vector index" in text or "read-only" in text or "read only" in text:
        return RuntimeError(
            f"{exc}\n\nThis looks like the earlier DiskANN index version, which makes the "
            "table read-only. Drop the index (sqlvec index --drop), ingest, then "
            "recreate it (sqlvec index)."
        )
    return exc


def ingest_path(
    conn: Any,
    root: str | Path,
    embedder: Embedder,
    *,
    dims: int,
    chunk_size: int = 1000,
    overlap: int = 150,
    extra_metadata: Mapping[str, str] | None = None,
    force: bool = False,
    prune: bool = False,
    progress: Callable[[str], None] | None = None,
) -> IngestReport:
    """Chunk, embed and store every supported file under ``root``.

    Files whose content hash hasn't changed are skipped (no embedding calls,
    no cost) unless ``force`` is set. Each file goes in its own transaction.
    """
    if embedder.dims != dims:
        raise ValueError(f"embedder makes {embedder.dims}-dim vectors but the column is {dims}")
    if prune and Path(root).is_file():
        raise ValueError("prune only makes sense when ingesting a folder")
    report = IngestReport()
    extra = dict(extra_metadata or {})
    say = progress or (lambda _msg: None)

    for doc in iter_documents(root):
        report.files_seen += 1
        report.sources.append(doc.source)
        current = db.fetchone(conn, GET_HASH_SQL, (doc.source,))
        if not force and current and str(current[0]) == doc.content_hash:
            report.files_unchanged += 1
            say(f"unchanged  {doc.source}")
            continue

        chunks = chunk_text(doc.text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            report.files_empty += 1
            say(f"empty      {doc.source}")
            continue

        texts = [c.text for c in chunks]
        vectors = embedder.embed(texts)
        try:
            with transaction(conn):
                upsert_document(
                    conn,
                    source=doc.source,
                    title=doc.title,
                    content_hash=doc.content_hash,
                    metadata={**doc.metadata, **extra},
                    texts=texts,
                    vectors=vectors,
                    dims=dims,
                    embedder_name=embedder.name,
                )
        except Exception as exc:
            raise _friendly(exc) from exc
        report.files_ingested += 1
        report.chunks_written += len(chunks)
        report.tokens_embedded += sum(c.token_estimate for c in chunks)
        say(f"ingested   {doc.source} ({len(chunks)} chunks)")

    if prune:
        with transaction(conn):
            report.files_deleted = delete_missing(conn, report.sources)
    return report
