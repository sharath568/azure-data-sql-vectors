"""Load the ``.sql`` files, fill in the few validated placeholders, split on GO.

The rule in this project: **user input never becomes SQL text.** Query
text, vectors, filters and k go through ``?`` bind parameters or stored
procedure parameters. The only things substituted into SQL text are:

* ``__DIMS__``  - an int in 1..1998, validated here
* ``__K__`` / ``__TOP_N__`` - ints in a fixed range, validated here
* ``__TABLE__`` - one of :data:`ALLOWED_TABLES`, never free text
* ``__FILTER__`` - one of two fixed strings chosen by sqlvec

T-SQL needs literals in a few of those places (column types, TOP in some
VECTOR_SEARCH forms, DDL), which is why they exist at all.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from sqlvec.config import MAX_VECTOR_DIMS, ConfigError

SCHEMA_SCRIPTS = (
    "001_schema.sql",
    "002_fulltext.sql",
    "003_exact_search.sql",
)
HYBRID_SCRIPT = "004_hybrid_search.sql"
VECTOR_INDEX_SCRIPT = "005_vector_index.sql"

ALLOWED_TABLES = ("dbo.sqlvec_chunks", "dbo.sqlvec_bench_items")
MAX_K = 1000
MAX_TOP_N = 100_000

FILTER_CLAUSE = "WHERE dbo.sqlvec_matches_filter(t.metadata, @filter) = 1"
NO_FILTER_CLAUSE = ""

_GO_RE = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


def sql_dir() -> Path:
    """Directory with the ``.sql`` files: packaged copy if installed, else the repo's."""
    try:
        packaged = Path(str(resources.files("sqlvec") / "sql"))
        if (packaged / SCHEMA_SCRIPTS[0]).is_file():
            return packaged
    except (ModuleNotFoundError, TypeError):  # pragma: no cover
        pass
    repo = Path(__file__).resolve().parents[2] / "sql"
    if (repo / SCHEMA_SCRIPTS[0]).is_file():
        return repo
    raise FileNotFoundError("cannot find sqlvec's sql/ directory")


def read_script(name: str) -> str:
    """Raw text of one script, e.g. ``"001_schema.sql"`` or ``"queries/ann_search.sql"``."""
    return (sql_dir() / name).read_text(encoding="utf-8")


def check_int(value: int, name: str, lo: int, hi: int) -> int:
    """Validate an int that will be written into SQL text."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an int, got {type(value).__name__}")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def check_table(table: str) -> str:
    """Only sqlvec's own tables can appear in SQL text."""
    if table not in ALLOWED_TABLES:
        raise ConfigError(f"table must be one of {ALLOWED_TABLES}, got {table!r}")
    return table


def render(
    text: str,
    *,
    dims: int | None = None,
    k: int | None = None,
    top_n: int | None = None,
    table: str | None = None,
    use_filter: bool | None = None,
) -> str:
    """Replace placeholders. Raises if any placeholder is left unfilled."""
    if dims is not None:
        text = text.replace("__DIMS__", str(check_int(dims, "dims", 1, MAX_VECTOR_DIMS)))
    if k is not None:
        text = text.replace("__K__", str(check_int(k, "k", 1, MAX_K)))
    if top_n is not None:
        text = text.replace("__TOP_N__", str(check_int(top_n, "top_n", 1, MAX_TOP_N)))
    if table is not None:
        text = text.replace("__TABLE__", check_table(table))
    if use_filter is not None:
        text = text.replace("__FILTER__", FILTER_CLAUSE if use_filter else NO_FILTER_CLAUSE)
    leftover = sorted(set(_PLACEHOLDER_RE.findall(strip_comments(text))))
    if leftover:
        raise ConfigError(f"unfilled placeholders in SQL: {', '.join(leftover)}")
    return text


def split_batches(text: str) -> list[str]:
    """Split a script on ``GO`` lines, like sqlcmd. Empty batches are dropped."""
    parts = _GO_RE.split(text)
    return [p.strip() for p in parts if strip_comments(p).strip()]


def strip_comments(text: str) -> str:
    """Drop whole-line ``--`` comments.

    Used for query templates, so a ``?`` in a comment is never mistaken for a
    bind parameter by the driver. Only full-line comments are removed; none of
    sqlvec's SQL puts ``--`` inside a string literal.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def schema_batches(dims: int) -> list[tuple[str, str]]:
    """``(script name, batch)`` pairs for the base schema, rendered for ``dims``."""
    out: list[tuple[str, str]] = []
    for name in SCHEMA_SCRIPTS:
        for batch in split_batches(render(read_script(name), dims=dims)):
            out.append((name, batch))
    return out


def script_batches(name: str, dims: int | None = None) -> list[str]:
    """Rendered batches of one script."""
    return split_batches(render(read_script(name), dims=dims))


def ann_query(
    table: str, dims: int, k: int, *, legacy: bool, use_filter: bool, top_n: int = 0
) -> str:
    """The VECTOR_SEARCH query for one table, in the new or the legacy syntax."""
    if legacy:
        text = read_script("queries/ann_search_legacy.sql")
        return strip_comments(
            render(text, dims=dims, k=k, top_n=max(top_n, k), table=table, use_filter=use_filter)
        )
    text = read_script("queries/ann_search.sql")
    return strip_comments(render(text, dims=dims, k=k, table=table, use_filter=use_filter))


def exact_bench_query(table: str, dims: int, k: int) -> str:
    """Exact top-k query used by the benchmark."""
    return strip_comments(
        render(read_script("queries/exact_bench.sql"), dims=dims, k=k, table=table)
    )
