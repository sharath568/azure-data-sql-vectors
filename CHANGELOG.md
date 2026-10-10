# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-10

First release.

### Added

- T-SQL scripts: `sqlvec_documents` and `sqlvec_chunks` with a native
  `VECTOR(n)` column (n validated to 1..1998), JSON metadata, and an INT
  clustered primary key so DiskANN can be used.
- Full-text catalog and index, created only when the server has full-text
  search installed.
- Stored procedures: `sqlvec_search_exact` (`VECTOR_DISTANCE` + `TOP`),
  `sqlvec_search_fts` (`FREETEXTTABLE`) and `sqlvec_search_hybrid`
  (Reciprocal Rank Fusion, k = 60, of exact vector and full-text ranks),
  all with JSON metadata filters.
- DiskANN index (`CREATE VECTOR INDEX`) and `VECTOR_SEARCH` queries that
  try `SELECT TOP (n) WITH APPROXIMATE` first and fall back to the earlier
  `TOP_N` form. Turns on `PREVIEW_FEATURES` on SQL Server 2025, skips with
  the engine's reason when unsupported.
- Optional Azure / SQL Server 2025 script for in-database embeddings with
  `CREATE EXTERNAL MODEL` + `AI_GENERATE_EMBEDDINGS`, and T-SQL-only
  ingestion with `AI_GENERATE_CHUNKS`.
- CLI: `init`, `index`, `doctor`, `ingest`, `search` (exact / ann / fts /
  hybrid), `bench` (recall@10, p50/p95, load and index build time,
  `sp_spaceused`; Markdown + JSON), `cost` (Azure SQL free offer
  vCore-seconds), `compare-pg` (T-SQL next to pgvector), `config`, `drop`.
- Embedders: Azure OpenAI `text-embedding-3-small` over REST and a free
  deterministic hash embedder.
- Bicep for an Azure SQL Database on the free offer (`useFreeLimit`,
  `AutoPause`), optional Azure OpenAI, budget alerts; `up.sh` / `down.sh`.
- Docker Compose for SQL Server 2025 Developer edition, plus a full-text
  image (`docker/fts`).
- GitHub Actions: ruff + unit tests on Python 3.10 to 3.12, integration
  tests and a 5k x 384 benchmark against the SQL Server 2025 container,
  a best-effort full-text job, and a Bicep build + lint.
