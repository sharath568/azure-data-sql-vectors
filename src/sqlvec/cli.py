"""``sqlvec`` command line interface."""

from __future__ import annotations

import dataclasses
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import click

from sqlvec import __version__
from sqlvec.config import ConfigError, Settings, load_dotenv

MODES = ("exact", "ann", "fts", "hybrid")


def parse_key_values(pairs: tuple[str, ...], option: str = "--filter") -> dict[str, str]:
    """Turn ``("team=docs", "lang=en")`` into ``{"team": "docs", "lang": "en"}``."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"expected key=value, got {pair!r}", param_hint=option)
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise click.BadParameter(f"empty key in {pair!r}", param_hint=option)
        out[key] = value.strip()
    return out


def _snippet(text: str, width: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def _settings(ctx: click.Context) -> Settings:
    settings = ctx.obj.get("settings") if ctx.obj else None
    if settings is None:  # pragma: no cover - always set by the group
        settings = Settings.from_env()
    return settings


def _fail(prefix: str, exc: Exception) -> click.ClickException:
    return click.ClickException(f"{prefix}: {' '.join(str(exc).split())}")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="sqlvec")
@click.option("--conn", envvar="SQLVEC_CONN", help="Connection string (or SQLVEC_CONN).")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=".env",
    show_default=True,
    help="Load variables from this file if it exists (never overrides real env vars).",
)
@click.pass_context
def cli(ctx: click.Context, conn: str | None, env_file: Path) -> None:
    """Native vector search in SQL Server 2025 and Azure SQL."""
    load_dotenv(env_file)
    ctx.ensure_object(dict)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if conn:
        settings = dataclasses.replace(settings, conn=conn)
    ctx.obj["settings"] = settings


# --------------------------------------------------------------------------
# setup


@cli.command()
@click.option(
    "--create-database",
    is_flag=True,
    help="Create the database from the connection string first (connects to master).",
)
@click.option(
    "--ann/--no-ann",
    default=False,
    show_default=True,
    help="Also create the DiskANN index (needs >= 100 chunks; preview on SQL Server 2025).",
)
@click.pass_context
def init(ctx: click.Context, create_database: bool, ann: bool) -> None:
    """Create tables, the full-text index (if available) and the search procedures."""
    from sqlvec import db
    from sqlvec.embeddings import get_embedder

    settings = _settings(ctx)
    try:
        embedder_name = get_embedder(settings).name
    except ConfigError:
        embedder_name = settings.embed_provider
    try:
        if create_database and db.create_database(settings.conn):
            click.echo("created database.")
        with db.connection(settings.conn) as conn:
            report = db.init_schema(
                conn, settings.embed_dims, embedder_name=embedder_name, vector_index=ann
            )
    except (db.SchemaError, db.UnsupportedError, ConfigError) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise _fail("init failed", exc) from exc

    caps = report.capabilities
    click.echo(f"{caps.platform} {caps.product_version}; VECTOR({settings.embed_dims})")
    for name in report.applied:
        click.echo(f"  applied {name}")
    click.echo(f"  full-text index: {'yes' if report.fulltext_index else 'no'}")
    if ann:
        click.echo(f"  DiskANN index: {report.vector_index}")
    for notice in report.notices:
        click.echo(f"  notice: {notice}")
    click.echo("ready.")


@cli.command()
@click.option("--drop", is_flag=True, help="Drop the vector index instead of creating it.")
@click.pass_context
def index(ctx: click.Context, drop: bool) -> None:
    """Create (or drop) the DiskANN vector index on the chunks table."""
    from sqlvec import db

    settings = _settings(ctx)
    try:
        with db.connection(settings.conn) as conn:
            if drop:
                click.echo("dropped." if db.drop_vector_index(conn) else "no vector index to drop.")
                return
            click.echo(f"DiskANN index: {db.create_vector_index(conn)}")
    except Exception as exc:
        raise _fail("index failed", exc) from exc


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Show what the connected engine supports (vector type, full-text, DiskANN)."""
    from sqlvec import db

    settings = _settings(ctx)
    try:
        with db.connection(settings.conn) as conn:
            caps = db.detect_capabilities(conn)
            meta = db.get_meta(conn)
            info = db.vector_index_info(conn) if meta else None
            ft = db.fulltext_index_exists(conn) if meta else False
            hybrid = db.hybrid_procs_exist(conn) if meta else False
    except Exception as exc:
        raise _fail("could not connect", exc) from exc

    def yn(flag: bool | None) -> str:
        return "unknown" if flag is None else ("yes" if flag else "no")

    click.echo(f"engine            : {caps.platform} {caps.product_version}")
    click.echo(f"VECTOR type       : {yn(caps.vector_type)}")
    click.echo(f"full-text         : {yn(caps.fulltext_installed)} (installed on server)")
    click.echo(f"PREVIEW_FEATURES  : {yn(caps.preview_features)}")
    if caps.vector_index_needs_preview and not caps.is_azure:
        click.echo("                    DiskANN / VECTOR_SEARCH are preview here and need it ON")
    click.echo(
        f"sqlvec schema     : {'VECTOR(' + meta['dims'] + ')' if meta else 'not initialised'}"
    )
    if meta:
        click.echo(f"full-text index   : {yn(ft)}; hybrid procs: {yn(hybrid)}")
        if info:
            click.echo(
                f"vector index      : {info.name} ({info.index_type}, {info.metric}, "
                f"version {info.version or 'unknown'})"
            )
        else:
            click.echo("vector index      : none")
    for note in caps.notes:
        click.echo(f"note: {note}")


@cli.command()
@click.option("--yes", is_flag=True, help="Really drop every sqlvec table and procedure.")
@click.pass_context
def drop(ctx: click.Context, yes: bool) -> None:
    """Drop all sqlvec objects (tables, procedures, full-text catalog)."""
    from sqlvec import db

    if not yes:
        raise click.ClickException("this deletes all sqlvec data; re-run with --yes")
    settings = _settings(ctx)
    try:
        with db.connection(settings.conn) as conn:
            db.drop_schema(conn)
    except Exception as exc:
        raise _fail("drop failed", exc) from exc
    click.echo("dropped.")


# --------------------------------------------------------------------------
# data


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--meta", "meta", multiple=True, help="Metadata key=value added to every chunk.")
@click.option("--force", is_flag=True, help="Re-embed files even if unchanged.")
@click.option("--prune", is_flag=True, help="Delete documents no longer present under PATH.")
@click.option("--chunk-size", type=int, default=None, help="Characters per chunk.")
@click.option("--overlap", type=int, default=None, help="Characters shared between chunks.")
@click.option("-q", "--quiet", is_flag=True, help="Only print the summary.")
@click.pass_context
def ingest(
    ctx: click.Context,
    path: Path,
    meta: tuple[str, ...],
    force: bool,
    prune: bool,
    chunk_size: int | None,
    overlap: int | None,
    quiet: bool,
) -> None:
    """Chunk, embed and store .md / .txt files from PATH."""
    from sqlvec import db
    from sqlvec.embeddings import EmbeddingError, get_embedder
    from sqlvec.ingest import ingest_path

    settings = _settings(ctx)
    extra = parse_key_values(meta, "--meta")
    try:
        embedder = get_embedder(settings)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        with db.connection(settings.conn) as conn:
            report = ingest_path(
                conn,
                path,
                embedder,
                dims=settings.embed_dims,
                chunk_size=chunk_size or settings.chunk_size,
                overlap=settings.chunk_overlap if overlap is None else overlap,
                extra_metadata=extra,
                force=force,
                prune=prune,
                progress=None if quiet else click.echo,
            )
            totals = db.counts(conn)
    except (EmbeddingError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise _fail("ingest failed", exc) from exc

    click.echo(
        f"{report.files_seen} files: {report.files_ingested} ingested, "
        f"{report.files_unchanged} unchanged, {report.files_empty} empty"
        + (f", {report.files_deleted} pruned" if prune else "")
        + f". {report.chunks_written} chunks written (~{report.tokens_embedded:,} tokens) "
        f"with {embedder.name}. Totals: {totals['documents']} docs, {totals['chunks']} chunks."
    )


def run_search(
    settings: Settings, query: str, mode: str, k: int, filters: dict[str, str]
) -> tuple[list[Any], str | None]:
    """Run a search; returns (results, notice). Falls back from hybrid to exact
    when the server has no full-text search, and says so."""
    from sqlvec import db
    from sqlvec.embeddings import get_embedder

    with db.connection(settings.conn) as conn:
        if mode == "fts":
            return db.search_fts(conn, query, k=k, filters=filters), None
        qvec = get_embedder(settings).embed([query])[0]
        dims = settings.embed_dims
        if mode == "exact":
            return db.search_exact(conn, qvec, dims, k=k, filters=filters), None
        if mode == "ann":
            return db.search_ann(conn, qvec, dims, k=k, filters=filters), None
        if not db.hybrid_procs_exist(conn):
            notice = (
                "full-text search isn't available in this database, so this ran as an "
                "exact vector search instead of hybrid."
            )
            return db.search_exact(conn, qvec, dims, k=k, filters=filters), notice
        return db.search_hybrid(conn, query, qvec, dims, k=k, filters=filters), None


@cli.command()
@click.argument("query")
@click.option("--mode", type=click.Choice(MODES), default="hybrid", show_default=True)
@click.option("-k", "k", type=click.IntRange(1, 100), default=5, show_default=True)
@click.option("--filter", "filters", multiple=True, help="Metadata key=value (repeatable).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.pass_context
def search(
    ctx: click.Context,
    query: str,
    mode: str,
    k: int,
    filters: tuple[str, ...],
    as_json: bool,
) -> None:
    """Search the ingested chunks (exact | ann | fts | hybrid)."""
    from sqlvec import db

    settings = _settings(ctx)
    try:
        results, notice = run_search(settings, query, mode, k, parse_key_values(filters))
    except (ConfigError, ValueError, db.UnsupportedError) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise _fail("search failed", exc) from exc

    if notice:
        click.echo(f"note: {notice}", err=True)
    if as_json:
        click.echo(json.dumps([r.to_dict() for r in results], indent=2))
        return
    if not results:
        click.echo("no results.")
        return
    for i, r in enumerate(results, start=1):
        extra = ""
        if r.vector_rank is not None or r.fts_rank is not None:
            extra = f"  (vector #{r.vector_rank or '-'}, fts #{r.fts_rank or '-'})"
        elif r.distance is not None:
            extra = f"  (cosine distance {r.distance:.4f})"
        click.echo(f"{i}. [{r.score:.4f}] {r.source}{extra}")
        click.echo(textwrap.indent(_snippet(r.content), "   "))


# --------------------------------------------------------------------------
# bench / cost / compare


@cli.command()
@click.option("-n", "n", type=click.IntRange(100), default=5_000, show_default=True)
@click.option("--dims", type=click.IntRange(2, 1998), default=384, show_default=True)
@click.option("--queries", type=click.IntRange(10), default=100, show_default=True)
@click.option("-k", "k", type=click.IntRange(1, 100), default=10, show_default=True)
@click.option("--ann/--no-ann", default=True, show_default=True, help="Try the DiskANN index.")
@click.option("--batch", type=click.IntRange(1, 10_000), default=500, show_default=True)
@click.option("--seed", type=int, default=42, show_default=True)
@click.option("--clusters", type=click.IntRange(1), default=64, show_default=True)
@click.option("--noise", type=click.FloatRange(0.0), default=1.0, show_default=True)
@click.option("--keep", is_flag=True, help="Keep the bench table afterwards.")
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Write <out>.md and <out>.json.",
)
@click.pass_context
def bench(
    ctx: click.Context,
    n: int,
    dims: int,
    queries: int,
    k: int,
    ann: bool,
    batch: int,
    seed: int,
    clusters: int,
    noise: float,
    keep: bool,
    out: Path | None,
) -> None:
    """Synthetic recall / latency benchmark: exact vs DiskANN."""
    from sqlvec import bench as bench_mod
    from sqlvec import db

    settings = _settings(ctx)
    try:
        with db.connection(settings.conn) as conn:
            result = bench_mod.run_benchmark(
                conn,
                n=n,
                dims=dims,
                queries=queries,
                k=k,
                seed=seed,
                clusters=clusters,
                noise=noise,
                ann=ann,
                batch=batch,
                keep=keep,
                progress=lambda msg: click.echo(msg, err=True),
            )
    except Exception as exc:
        raise _fail("bench failed", exc) from exc

    md = bench_mod.to_markdown(result)
    click.echo(md)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".md").write_text(md, encoding="utf-8")
        out.with_suffix(".json").write_text(result.to_json() + "\n", encoding="utf-8")
        click.echo(f"wrote {out.with_suffix('.md')} and {out.with_suffix('.json')}", err=True)


@cli.command()
@click.option(
    "--sessions",
    type=click.IntRange(0),
    default=20,
    show_default=True,
    help="Times per month you wake the database up.",
)
@click.option(
    "--active-minutes",
    type=click.FloatRange(0),
    default=30.0,
    show_default=True,
    help="Minutes of real work per session.",
)
@click.option(
    "--active-vcores",
    type=click.FloatRange(0),
    default=1.0,
    show_default=True,
    help="Average vCores used while working (billing never goes below the floor).",
)
@click.option(
    "--autopause-delay",
    type=click.IntRange(15, 10_080),
    default=60,
    show_default=True,
    help="Minutes idle before the database pauses (15 is the minimum).",
)
@click.option(
    "--sku",
    type=click.Choice(["GP_S_Gen5_1", "GP_S_Gen5_2", "GP_S_Gen5_4"]),
    default="GP_S_Gen5_2",
    show_default=True,
)
@click.option("--min-vcores", type=click.FloatRange(0.5, 4), default=0.5, show_default=True)
@click.option("--chunks", type=click.IntRange(0), default=10_000, show_default=True)
@click.option("--dims", type=click.IntRange(1, 1998), default=1536, show_default=True)
@click.option(
    "--queries",
    type=click.IntRange(0),
    default=1000,
    show_default=True,
    help="Embedded search queries per month.",
)
@click.option("--json", "as_json", is_flag=True)
def cost(
    sessions: int,
    active_minutes: float,
    active_vcores: float,
    autopause_delay: int,
    sku: str,
    min_vcores: float,
    chunks: int,
    dims: int,
    queries: int,
    as_json: bool,
) -> None:
    """Will a demo fit in the Azure SQL free offer? (vCore-seconds math; verify prices)."""
    from sqlvec import cost as cost_mod

    usage = cost_mod.Usage(
        sessions=sessions,
        active_minutes=active_minutes,
        active_vcores=active_vcores,
        autopause_delay_min=autopause_delay,
        sku=sku,
        min_vcores=min_vcores,
        chunks=chunks,
        dims=dims,
        queries=queries,
    )
    try:
        est = cost_mod.estimate(usage)
        by_delay = cost_mod.delay_table(usage)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    "note": cost_mod.PRICES_NOTE,
                    "usage": dataclasses.asdict(usage),
                    "estimate": est.to_dict(),
                    "by_autopause_delay": {str(d): e.to_dict() for d, e in by_delay},
                },
                indent=2,
            )
        )
        return
    click.echo(cost_mod.to_markdown(usage, est))
    click.echo()
    click.echo("Same usage, different auto-pause delay:")
    click.echo()
    click.echo("| delay (min) | vCore-seconds | % of free | sessions that fit |")
    click.echo("|---:|---:|---:|---:|")
    for d, e in by_delay:
        click.echo(
            f"| {d} | {e.total_vcore_seconds:,.0f} | {e.percent_of_free:g}% "
            f"| {e.sessions_that_fit} |"
        )


@cli.command(name="compare-pg")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "markdown"]),
    default="text",
    show_default=True,
)
@click.option("--no-notes", is_flag=True, help="Markdown without the notes column.")
def compare_pg(fmt: str, no_notes: bool) -> None:
    """Print each operation in T-SQL next to its pgvector equivalent."""
    from sqlvec import compare

    if fmt == "markdown":
        click.echo(compare.to_markdown(notes=not no_notes), nl=False)
    else:
        click.echo(compare.to_text(), nl=False)


@cli.command(name="config")
@click.pass_context
def show_config(ctx: click.Context) -> None:
    """Print the effective settings (secrets masked)."""
    click.echo(json.dumps(_settings(ctx).redacted(), indent=2))


def main() -> None:
    """Console script entry point."""
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
