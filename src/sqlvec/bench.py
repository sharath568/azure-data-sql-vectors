"""Synthetic vector benchmark: exact VECTOR_DISTANCE vs DiskANN VECTOR_SEARCH.

What it does:

1. Generates ``n`` clustered, unit-length float32 vectors plus ``queries``
   held-out query vectors (fixed seed, so runs repeat).
2. Computes the exact top-k neighbours in NumPy. That's the ground truth,
   independent of the database.
3. Loads the vectors into a scratch table (batches of JSON arrays unpacked
   server side with OPENJSON) and times the load.
4. Times an exact top-k query (``ORDER BY VECTOR_DISTANCE``) for every query
   vector and checks its recall against NumPy (it should be 1.0; anything
   else means a bug or float32 ties).
5. If the engine allows it, builds a DiskANN index (``CREATE VECTOR INDEX``),
   times the build, and measures recall@k and latency for ``VECTOR_SEARCH``.
   On SQL Server 2025 that needs PREVIEW_FEATURES; if the engine refuses, the
   result says so and the ANN rows are simply absent.
6. Records table size before and after the index with ``sp_spaceused``.

Latency is wall-clock time in the client around execute + fetch, so it
includes the network round trip. Tiny on localhost; not tiny against Azure.
Queries run one at a time, so this says nothing about throughput.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from sqlvec import db, sqlscripts, vecjson

BENCH_TABLE = "dbo.sqlvec_bench_items"
BENCH_INDEX = "vix_sqlvec_bench_items"


@dataclass(frozen=True)
class LatencyStats:
    """Latency summary in milliseconds."""

    p50_ms: float
    p95_ms: float
    mean_ms: float


@dataclass
class SpaceUsed:
    """``sp_spaceused`` for one table, in KB."""

    rows: int
    reserved_kb: int
    data_kb: int
    index_kb: int
    unused_kb: int


@dataclass
class BenchResult:
    """Everything one benchmark run measured."""

    n: int
    dims: int
    queries: int
    k: int
    seed: int
    clusters: int
    noise: float
    environment: dict[str, Any]
    load_seconds: float
    space_before_index: SpaceUsed | None
    exact_latency: LatencyStats
    exact_recall: float
    ann_build_seconds: float | None = None
    ann_index_version: str | None = None
    ann_syntax: str | None = None
    ann_latency: LatencyStats | None = None
    ann_recall: float | None = None
    space_after_index: SpaceUsed | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# --------------------------------------------------------------------------
# pure helpers (unit tested)


def synthetic_dataset(
    n: int,
    queries: int,
    dims: int,
    seed: int = 42,
    clusters: int = 64,
    noise: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Clustered unit-length float32 data (``n`` x ``dims``) plus held-out queries.

    Data and queries are drawn around the same random cluster centres, so
    queries land near the data without being copies of it. Clustered data is
    closer to real embeddings than uniform noise.
    """
    if n < 1 or dims < 1 or queries < 1:
        raise ValueError("n, queries and dims must be positive")
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((clusters, dims)).astype(np.float32)

    def draw(count: int) -> np.ndarray:
        assign = rng.integers(0, clusters, size=count)
        noise_part = noise * rng.standard_normal((count, dims)).astype(np.float32)
        return normalize_rows(centres[assign] + noise_part)

    return draw(n), draw(queries)


def normalize_rows(x: np.ndarray) -> np.ndarray:
    """Scale each row to unit length (zero rows stay zero)."""
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (x / norms).astype(np.float32)


def exact_topk(data: np.ndarray, queries: np.ndarray, k: int, block: int = 256) -> np.ndarray:
    """Exact top-k ids by cosine similarity (inputs must be unit length).

    Returns shape ``(len(queries), k)`` with 1-based ids to match the table.
    """
    k = min(k, len(data))
    out = np.empty((len(queries), k), dtype=np.int64)
    for start in range(0, len(queries), block):
        sims = queries[start : start + block] @ data.T
        part = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        part_sims = np.take_along_axis(sims, part, axis=1)
        order = np.argsort(-part_sims, axis=1, kind="stable")
        out[start : start + block] = np.take_along_axis(part, order, axis=1) + 1
    return out


def recall_at_k(found: Sequence[Sequence[int]], truth: np.ndarray, k: int) -> float:
    """Mean fraction of the true top-k that each query found."""
    if len(found) != len(truth):
        raise ValueError("found and truth must have the same number of queries")
    if len(found) == 0:
        return 0.0
    total = 0.0
    for got, exp in zip(found, truth, strict=True):
        exp_set = {int(x) for x in exp[:k]}
        total += len(exp_set.intersection(int(x) for x in list(got)[:k])) / max(1, len(exp_set))
    return total / len(found)


def latency_stats(samples_seconds: Sequence[float]) -> LatencyStats:
    """p50 / p95 / mean in milliseconds."""
    if not samples_seconds:
        return LatencyStats(0.0, 0.0, 0.0)
    ms = np.asarray(samples_seconds, dtype=np.float64) * 1000.0
    return LatencyStats(
        p50_ms=round(float(np.percentile(ms, 50)), 3),
        p95_ms=round(float(np.percentile(ms, 95)), 3),
        mean_ms=round(float(ms.mean()), 3),
    )


_KB_RE = re.compile(r"^\s*(\d+)\s*KB\s*$", re.IGNORECASE)


def parse_kb(text: Any) -> int:
    """``'1234 KB'`` -> 1234. sp_spaceused reports sizes as text."""
    if text is None:
        return 0
    m = _KB_RE.match(str(text))
    if not m:
        raise ValueError(f"unexpected sp_spaceused size {text!r}")
    return int(m.group(1))


def parse_spaceused(row: Sequence[Any]) -> SpaceUsed:
    """Row of ``sp_spaceused 'table'``: name, rows, reserved, data, index_size, unused."""
    return SpaceUsed(
        rows=int(str(row[1]).strip()),
        reserved_kb=parse_kb(row[2]),
        data_kb=parse_kb(row[3]),
        index_kb=parse_kb(row[4]),
        unused_kb=parse_kb(row[5]),
    )


def load_sql(dims: int) -> str:
    """Batch insert: one JSON array of vectors per call, unpacked by OPENJSON.

    Parameters: id offset (int), JSON array of arrays (text). ``[key]`` is the
    0-based array position, so ids come out as offset + position.
    """
    sqlscripts.check_int(dims, "dims", 1, 1998)
    return (
        f"INSERT INTO {BENCH_TABLE} (id, embedding) "
        f"SELECT ? + CAST(j.[key] AS INT), CAST(j.[value] AS VECTOR({dims})) "
        "FROM OPENJSON(?) AS j"
    )


def create_table_sql(dims: int) -> list[str]:
    sqlscripts.check_int(dims, "dims", 1, 1998)
    return [
        f"DROP TABLE IF EXISTS {BENCH_TABLE}",
        f"CREATE TABLE {BENCH_TABLE} ("
        "id INT NOT NULL CONSTRAINT PK_sqlvec_bench_items PRIMARY KEY CLUSTERED, "
        f"embedding VECTOR({dims}) NOT NULL)",
    ]


CREATE_INDEX_SQL = (
    f"CREATE VECTOR INDEX {BENCH_INDEX} ON {BENCH_TABLE} (embedding) "
    "WITH (METRIC = 'cosine', TYPE = 'DiskANN')"
)


# --------------------------------------------------------------------------
# database parts


def environment_info(conn: Any, caps: db.Capabilities) -> dict[str, Any]:
    """Server and client details worth writing next to any number."""
    env: dict[str, Any] = {
        "platform": caps.platform,
        "product_version": caps.product_version,
        "engine_edition": caps.engine_edition,
        "version_banner": caps.version_banner,
        "preview_features": caps.preview_features,
        "client_platform": platform.platform(),
        "client_python": platform.python_version(),
        "client_cpu_count": os.cpu_count(),
    }
    try:
        row = db.fetchone(conn, "SELECT cpu_count, physical_memory_kb FROM sys.dm_os_sys_info")
        if row:
            env["server_cpu_count"] = int(row[0])
            env["server_memory_gib"] = round(int(row[1]) / 1024 / 1024, 1)
    except Exception:
        env["server_cpu_count"] = None  # needs VIEW SERVER STATE; not on every SKU
    try:
        from importlib.metadata import version

        env["driver"] = f"mssql-python {version('mssql-python')}"
    except Exception:  # pragma: no cover
        env["driver"] = "mssql-python"
    return env


def _spaceused(conn: Any) -> SpaceUsed | None:
    try:
        row = db.fetchone(conn, "EXEC sp_spaceused ?", (BENCH_TABLE,))
    except Exception:
        return None
    return parse_spaceused(row) if row else None


def _load(conn: Any, data: np.ndarray, batch: int) -> float:
    dims = data.shape[1]
    for stmt in create_table_sql(dims):
        db.run(conn, stmt)
    sql = load_sql(dims)
    t0 = time.perf_counter()
    for start in range(0, len(data), batch):
        payload = vecjson.many_to_json(data[start : start + batch], dims)
        db.run(conn, sql, (start + 1, payload))
    return time.perf_counter() - t0


def _timed(
    run_one: Callable[[np.ndarray], list[int]], queries: np.ndarray, warmup: int = 5
) -> tuple[list[list[int]], list[float]]:
    for q in queries[:warmup]:
        run_one(q)
    found: list[list[int]] = []
    samples: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        ids = run_one(q)
        samples.append(time.perf_counter() - t0)
        found.append(ids)
    return found, samples


def run_benchmark(
    conn: Any,
    n: int = 5_000,
    dims: int = 384,
    queries: int = 100,
    k: int = 10,
    seed: int = 42,
    clusters: int = 64,
    noise: float = 1.0,
    ann: bool | None = None,
    batch: int = 500,
    keep: bool = False,
    progress: Callable[[str], None] | None = None,
) -> BenchResult:
    """Run the benchmark on an autocommit connection. See the module docstring."""
    say = progress or (lambda _msg: None)
    sqlscripts.check_int(dims, "dims", 1, 1998)
    sqlscripts.check_int(k, "k", 1, sqlscripts.MAX_K)
    caps = db.detect_capabilities(conn)
    if not caps.vector_type:
        raise db.UnsupportedError(f"{caps.platform} has no VECTOR type")

    say(f"generating {n:,} x {dims} vectors and {queries} queries (seed {seed})")
    data, qvecs = synthetic_dataset(n, queries, dims, seed=seed, clusters=clusters, noise=noise)
    truth = exact_topk(data, qvecs, k)

    say(f"loading in batches of {batch} (JSON + OPENJSON)")
    load_seconds = _load(conn, data, batch)
    result = BenchResult(
        n=n,
        dims=dims,
        queries=queries,
        k=k,
        seed=seed,
        clusters=clusters,
        noise=noise,
        environment=environment_info(conn, caps),
        load_seconds=round(load_seconds, 3),
        space_before_index=_spaceused(conn),
        exact_latency=LatencyStats(0, 0, 0),
        exact_recall=0.0,
    )

    say("exact search (VECTOR_DISTANCE, full scan)")
    exact_sql = sqlscripts.exact_bench_query(BENCH_TABLE, dims, k)

    def exact_one(q: np.ndarray) -> list[int]:
        return [int(r[0]) for r in db.fetchall(conn, exact_sql, (vecjson.to_json(q, dims),))]

    found, samples = _timed(exact_one, qvecs)
    result.exact_latency = latency_stats(samples)
    result.exact_recall = round(recall_at_k(found, truth, k), 4)
    say(f"  exact: recall@{k}={result.exact_recall:.3f} p50={result.exact_latency.p50_ms}ms")

    want_ann = True if ann is None else ann
    if want_ann:
        _bench_ann(conn, caps, result, qvecs, truth, say)
    else:
        result.notes.append("ANN skipped (--no-ann).")

    if not keep:
        db.run(conn, f"DROP TABLE IF EXISTS {BENCH_TABLE}")
    return result


def _bench_ann(
    conn: Any,
    caps: db.Capabilities,
    result: BenchResult,
    qvecs: np.ndarray,
    truth: np.ndarray,
    say: Callable[[str], None],
) -> None:
    if result.n < db.MIN_ROWS_FOR_VECTOR_INDEX:
        result.notes.append(
            f"ANN skipped: CREATE VECTOR INDEX needs at least {db.MIN_ROWS_FOR_VECTOR_INDEX} rows."
        )
        return
    preview = db.enable_preview_features(conn, caps)
    result.environment["preview_features"] = caps.preview_features
    say(f"building DiskANN index ({preview})")
    t0 = time.perf_counter()
    try:
        db.run(conn, CREATE_INDEX_SQL)
    except Exception as exc:
        result.notes.append(
            f"DiskANN index not available on {caps.platform}: {db._short(exc)} "
            f"(PREVIEW_FEATURES: {preview})"
        )
        say("  DiskANN unavailable, see notes")
        return
    result.ann_build_seconds = round(time.perf_counter() - t0, 3)
    info = db.vector_index_info(conn, BENCH_TABLE)
    result.ann_index_version = info.version if info else None
    result.space_after_index = _spaceused(conn)
    result.notes.append(
        "sp_spaceused is reported as-is. I haven't confirmed from the docs whether it "
        "counts the DiskANN graph's internal storage, so compare before/after with care."
    )

    syntax_used: dict[str, bool] = {}

    def ann_one(q: np.ndarray) -> list[int]:
        hits, legacy = db.ann_ids(conn, BENCH_TABLE, q, result.dims, result.k)
        syntax_used["legacy"] = legacy
        return [h[0] for h in hits]

    try:
        found, samples = _timed(ann_one, qvecs)
    except db.UnsupportedError as exc:
        result.notes.append(f"VECTOR_SEARCH failed after the index was built: {exc}")
        return
    result.ann_syntax = (
        "VECTOR_SEARCH with TOP_N (earlier index version)"
        if syntax_used.get("legacy")
        else "SELECT TOP (k) WITH APPROXIMATE ... VECTOR_SEARCH"
    )
    result.ann_latency = latency_stats(samples)
    result.ann_recall = round(recall_at_k(found, truth, result.k), 4)
    say(f"  DiskANN: recall@{result.k}={result.ann_recall:.3f} p50={result.ann_latency.p50_ms}ms")


# --------------------------------------------------------------------------
# report


def _fmt_kb(kb: int | None) -> str:
    if kb is None:
        return "-"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.2f} GiB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MiB"
    return f"{kb} KiB"


def to_markdown(r: BenchResult) -> str:
    """Human-readable report. Every number here was measured in this run."""
    env = r.environment
    lines = [
        f"## sqlvec bench: {r.n:,} x {r.dims} float32, {r.queries} queries, k={r.k}",
        "",
        f"- Engine: {env.get('platform')} {env.get('product_version')}",
        f"- Server CPUs / memory: {env.get('server_cpu_count', '?')} / "
        f"{env.get('server_memory_gib', '?')} GiB",
        f"- Client: {env.get('client_platform')}, Python {env.get('client_python')}, "
        f"{env.get('driver')}",
        f"- Data: {r.clusters} clusters, noise {r.noise}, seed {r.seed}",
        f"- Load time: {r.load_seconds:.2f} s",
    ]
    if r.ann_build_seconds is not None:
        lines.append(
            f"- DiskANN build time: {r.ann_build_seconds:.2f} s "
            f"(index version {r.ann_index_version or 'unknown'})"
        )
    lines += [
        "",
        "| search | recall@k | p50 ms | p95 ms | mean ms |",
        "|---|---:|---:|---:|---:|",
        f"| exact (VECTOR_DISTANCE) | {r.exact_recall:.3f} | {r.exact_latency.p50_ms:.2f} | "
        f"{r.exact_latency.p95_ms:.2f} | {r.exact_latency.mean_ms:.2f} |",
    ]
    if r.ann_latency is not None and r.ann_recall is not None:
        lines.append(
            f"| DiskANN (VECTOR_SEARCH) | {r.ann_recall:.3f} | {r.ann_latency.p50_ms:.2f} | "
            f"{r.ann_latency.p95_ms:.2f} | {r.ann_latency.mean_ms:.2f} |"
        )
    lines += ["", "| sp_spaceused | rows | reserved | data | index |", "|---|---:|---:|---:|---:|"]
    for label, s in (("before index", r.space_before_index), ("after index", r.space_after_index)):
        if s is not None:
            lines.append(
                f"| {label} | {s.rows:,} | {_fmt_kb(s.reserved_kb)} | {_fmt_kb(s.data_kb)} | "
                f"{_fmt_kb(s.index_kb)} |"
            )
    if r.ann_syntax:
        lines += ["", f"ANN query form: {r.ann_syntax}."]
    if r.notes:
        lines += ["", "Notes:", ""] + [f"- {n}" for n in r.notes]
    lines += [
        "",
        "Latency is client-side wall time per query (execute + fetch), one query at a time.",
        "",
    ]
    return "\n".join(lines)
