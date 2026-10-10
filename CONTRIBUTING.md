# Contributing

Thanks for taking a look. This is a small reference project, so the bar is
"clear, tested, cheap to run, and honest about what's preview".

## Setup

```bash
docker compose up -d          # SQL Server 2025 Developer on localhost,1433
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sqlvec init --create-database
```

## Before you open a PR

```bash
ruff check . && ruff format --check .
pytest -m "not integration"
export SQLVEC_TEST_CONN="$(grep '^# SQLVEC_TEST_CONN=' .env.example | cut -d= -f2-)"
pytest -m integration -rs
```

CI runs the same checks on Python 3.10 to 3.12, the integration tests and a
small benchmark against `mcr.microsoft.com/mssql/server:2025-latest`, a
best-effort full-text job, and a Bicep build + lint of `infra/main.bicep`.

## Guidelines

- **No user input in SQL text.** Query text, vectors, filters, k and file
  contents travel as `?` parameters or stored procedure parameters. The
  only things written into SQL text are validated integers (`__DIMS__`,
  `__K__`, `__TOP_N__`), sqlvec's own table names, and fixed filter
  clauses. See `src/sqlvec/sqlscripts.py`; there are unit tests for it.
- **SQL lives in `sql/`** as plain files a DBA can read and run with
  sqlcmd or SSMS (replace `__DIMS__` by hand).
- **Preview features are detected, not assumed.** If the engine refuses
  something (DiskANN on SQL Server 2025 without `PREVIEW_FEATURES`,
  full-text without `mssql-server-fts`), sqlvec says so and carries on
  where it can. Tests skip with the engine's own message.
- **Keep the default path free.** Anything that calls a paid API must be
  opt-in and must fail with a clear message when it isn't configured.
- **Benchmarks:** only real numbers, with where they came from (CU,
  edition, VM / container size, or Azure SKU and region). Commit the JSON
  from `sqlvec bench --out` under `docs/bench/`.
- **Facts about the product** (limits, GA vs preview) go in
  `docs/sources.md` with a link and the date you checked.
- **Prices** in `src/sqlvec/cost.py` are reference constants. If you update
  them, say where they came from and when.

## Most useful contributions right now

1. A `sqlvec bench` run on Azure SQL Database (free offer is enough for
   5k to 50k vectors), including the DiskANN index version `doctor` reports.
2. Confirmation of which SQL Server 2025 CU builds the version 3 DiskANN
   index (DML-capable) versus the earlier read-only one.
3. A `pyodbc` code path, tested.
