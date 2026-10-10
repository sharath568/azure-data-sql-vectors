# Sources and what they say (checked 2026-10-10)

Everything sqlvec claims about GA vs preview, limits and syntax comes from
the pages below. Microsoft Learn is the authority; where it was silent or
unclear I say so, and the code says so too. Features move fast here, so if
you read this months later, re-check the pages, especially the preview items.

"Last updated" is the date shown on the page when I read it.

## The vector data type

**[vector data type](https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type)**
(last updated 2026-09-29)

- Stored "in an optimized binary format but exposed as JSON arrays";
  each element is "a single-precision (4-byte) floating-point value".
- **float32 (default): up to 1,998 dimensions. float16: up to 3,996.**
  Syntax `VECTOR(dimensions [, base_type])`, e.g. `VECTOR(3072, float16)`.
- **float16 is GA** in Azure SQL Database, Azure SQL Managed Instance and
  SQL database in Fabric, and **preview in SQL Server 2025**, where it needs
  `ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON`.
- Available under all compatibility levels. On Managed Instance, vector
  features need the "SQL Server 2025" or "Always-up-to-date" update policy.
- Constraints: only `NULL` / `NOT NULL` on a vector column (no `DEFAULT`,
  `CHECK`, primary/foreign keys). No B-tree or columnstore index on it. No
  memory-optimized tables, no Always Encrypted, no `sql_variant`, no alias
  types. `sp_describe_first_result_set` reports it as (n)varchar.
- **Drivers:** clients on TDS 7.4+ with updated drivers can send and read
  vectors in binary. Named: Microsoft.Data.SqlClient 6.1.0 and the JDBC
  driver 13.1.0 preview; float16 binary transport needs JDBC 13.4 or
  **ODBC Driver 18.7.1**. Everything else gets `varchar(max)` JSON and "the
  SQL Database Engine automatically converts vectors to and from a JSON
  array". No Python driver is mentioned, so sqlvec sends JSON text and casts
  it server side (`CAST(? AS VECTOR(n))`). mssql-python 1.15.0 bundles ODBC
  driver 18.6, below the 18.7.1 named for float16, which is another reason
  to stick to JSON.

## Vector functions

**[Vector functions](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-functions-transact-sql)**
(last updated 2026-09-29)

- `VECTOR_DISTANCE`, `VECTOR_NORM`, `VECTOR_NORMALIZE`, `VECTORPROPERTY`
  are GA in SQL Server 2025, Azure SQL Database, Managed Instance and Fabric.
- `VECTOR_SEARCH`: GA in Azure SQL Database, Fabric, and MI on the
  Always-up-to-date policy; **preview in SQL Server 2025** and MI on the
  SQL Server 2025 policy.

**[VECTOR_DISTANCE](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-distance-transact-sql)**
(last updated 2025-11-18)

- `VECTOR_DISTANCE(metric, v1, v2)`: `cosine` (0..2), `euclidean` (0..inf),
  `dot` = **negative** dot product (smaller is more similar). Returns float.
- "Vector distance is always exact and doesn't use any vector index, even
  if available." Approximate search goes through `VECTOR_SEARCH`.

**[VECTOR_NORMALIZE](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-normalize-transact-sql)**

- `VECTOR_NORMALIZE(vector, norm_type)` with `norm1`, `norm2`, `norminf`.
  (`VECTOR_NORM` takes the same norm types per the functions index; I didn't
  open its page separately.)

**[Vectors in the SQL Database Engine (overview)](https://learn.microsoft.com/sql/relational-databases/vectors/vectors-sql-server)**
(last updated 2026-09-15)

- Exact search is recommended for fewer than about **50,000 vectors**, or
  more when other predicates cut the candidates down to that range.

## Vector index (DiskANN) and VECTOR_SEARCH

**[CREATE VECTOR INDEX](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-vector-index-transact-sql)**
(last updated 2026-09-29)

- Syntax: `CREATE VECTOR INDEX name ON table (col) WITH (METRIC = 'cosine' |
  'dot' | 'euclidean', TYPE = 'DiskANN', MAXDOP = n)`. DiskANN is the only
  (and default) type.
- **GA** in Azure SQL Database, SQL database in Fabric, and MI with the
  Always-up-to-date policy. **Preview** in SQL Server 2025 and MI with the
  SQL Server 2025 policy, which need `PREVIEW_FEATURES = ON` ("not needed
  for Azure SQL Database or SQL database in Fabric"). No trace flags.
- Requirements: a base table (no views / temp tables), a **clustered
  primary key on an int column**, **at least 100 rows** with non-NULL
  vectors (otherwise Msg 42266). Not partitioned, not replicated to
  subscribers, no `TRUNCATE TABLE`, no DacPac (BACPAC from DacFx 170.5.96).
- **Read-only question:** with the **latest index version** "the table is
  no longer read-only"; INSERT/UPDATE/DELETE/MERGE work and the index is
  maintained, filters apply during the search, and the optimizer may choose
  kNN instead of the index. With **earlier versions** the table is
  read-only after index creation (or you opt into stale results with
  `ALLOW_STALE_VECTOR_INDEX`), filters apply after retrieval, and you tune
  `TOP_N` by hand. The version shows in
  `JSON_VALUE(sys.vector_indexes.build_parameters, '$.Version')`, latest = 3.
  Earlier indexes "will be retired"; drop and recreate to upgrade.
- **Unclear:** the page doesn't say which index version a given SQL Server
  2025 cumulative update builds. sqlvec reads the version after creating
  the index, tries the new query syntax first and falls back to the old.

**[VECTOR_SEARCH](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-search-transact-sql)**
(last updated 2026-09-29)

- Latest syntax: `SELECT TOP (N) WITH APPROXIMATE ... FROM VECTOR_SEARCH(
  TABLE = t AS s, COLUMN = c, SIMILAR_TO = @qv, METRIC = 'cosine') AS r
  ORDER BY r.distance`. `ORDER BY distance` ascending is required.
- Earlier syntax adds `TOP_N = k` inside the function; with a version 3
  index that raises Msg 42274.
- The index is only used if one exists on that column with the same metric;
  otherwise a warning and a full kNN scan (it doesn't fail).
- No `CROSS APPLY` / `OUTER APPLY` in the same `FROM`; not allowed in views.

**[sys.vector_indexes](https://learn.microsoft.com/sql/relational-databases/system-catalog-views/sys-vector-indexes-transact-sql)**

- Inherits `sys.indexes` columns, plus `vector_index_type`,
  `distance_metric`, `build_parameters` ("internal usage only").

**[DiskANN vector index improvements](https://devblogs.microsoft.com/azure-sql/diskann-vector-index-improvements/)**
(Azure SQL dev blog, 2026-03-18)

- The new version (DML, iterative filtering, `TOP (N) WITH APPROXIMATE`) was
  "rolling out across Azure SQL Database and SQL database in Microsoft
  Fabric". Indexes from the November 2025 preview use the old structure.
  The post doesn't cover SQL Server 2025.

## In-database AI: embeddings and chunking

**[AI_GENERATE_EMBEDDINGS](https://learn.microsoft.com/sql/t-sql/functions/ai-generate-embeddings-transact-sql)**
(last updated 2026-06-22)

- `AI_GENERATE_EMBEDDINGS(source USE MODEL model [PARAMETERS json])`, using
  an external model of type `EMBEDDINGS` from `CREATE EXTERNAL MODEL`.
- Needs `sp_configure 'external rest endpoint enabled', 1` on SQL Server
  2025 and MI; it's on by default in Azure SQL Database and Fabric.
- API-key auth goes through a database scoped credential named after the
  endpoint, `IDENTITY = 'HTTPEndpointHeaders'`, `SECRET = '{"api-key":"..."}'`.
  Managed identity is also supported.

**[AI_GENERATE_EMBEDDINGS and external models are GA](https://devblogs.microsoft.com/azure-sql/generate-embeddings-function-and-external-model-object-support-are-now-generally-available-in-azure-sql/)**
(Azure SQL dev blog, 2026-05-08)

- GA for Azure SQL Database, Azure SQL Managed Instance (both update
  policies) and SQL Server 2025.

**[AI functions](https://learn.microsoft.com/sql/t-sql/functions/ai-functions-transact-sql)**
(last updated 2026-08-24)

- `AI_GENERATE_CHUNKS` applies to SQL Server 2025, Azure SQL Database and
  SQL database in Fabric (**Managed Instance is not listed**).
- **Unclear:** I found no explicit GA/preview statement for
  `AI_GENERATE_CHUNKS`. The GA post above lists it among complementary
  features without a preview label (`VECTOR_SEARCH` next to it is labelled
  preview). A [Redgate Simple Talk article](https://www.red-gate.com/simple-talk/?p=110100)
  (2026-05-01) describes `CHUNK_TYPE = FIXED` as the only chunk type, an
  optional `OVERLAP` percentage (0 to 50), and output columns including
  `chunk`, `chunk_order`, `chunk_offset`, `chunk_length`. Treat that as
  secondary.

## Full-text search

**[FREETEXTTABLE](https://learn.microsoft.com/en-us/sql/relational-databases/system-functions/freetexttable-transact-sql)**

- Applies to SQL Server, Azure SQL Database, Azure SQL Managed Instance.
  Returns `[KEY]` and `[RANK]`; takes free text (no query syntax), optional
  `top_n_by_rank`.

**[Install SQL Server Full-Text Search on Linux](https://learn.microsoft.com/sql/linux/sql-server-linux-setup-full-text-search)**

- On Linux full-text is the separate `mssql-server-fts` package.
- The official container image doesn't include it. Microsoft's docs don't
  say this directly on the pages I read; it's the long-standing behaviour
  reported in the [GitHub community thread](https://github.community/t/ms-sql-service-with-full-text-search/137345)
  and the reason so many "mssql + fts" images exist. sqlvec checks
  `FULLTEXTSERVICEPROPERTY('IsFullTextInstalled')` and degrades instead of
  failing, and CI tests full-text in a separate best-effort job.

## Azure SQL Database free offer and serverless billing

**[Try Azure SQL Database for free](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer)**
(last updated 2026-03-18)

- Up to **10 General Purpose serverless databases per subscription**; each
  gets **100,000 vCore-seconds, 32 GB data and 32 GB backup storage per
  month**, for the life of the subscription.
- When the allowance is used up: "Auto-pause the database until next
  month" (max 4 vCores, 32 GB) or "Continue using database for additional
  charges". **You can't switch back to auto-pause** after choosing continue.
- The page doesn't say how vCore-seconds are metered (see the billing page).

**[ARM/Bicep: Microsoft.Sql/servers/databases](https://learn.microsoft.com/en-us/azure/templates/microsoft.sql/2023-08-01-preview/servers/databases)**

- `useFreeLimit` (bool, one database per subscription per the property
  description, which conflicts with the 10-database limit above; the offer
  page is newer) and `freeLimitExhaustionBehavior`: `AutoPause` |
  `BillOverUsage`. Also `autoPauseDelay` (minutes, -1 disables),
  `minCapacity`, `maxSizeBytes`. Bicep 0.48.1 accepts these on API version
  2023-08-01 without warnings.

**[Serverless compute tier](https://learn.microsoft.com/en-us/azure/azure-sql/database/serverless-tier-overview)**
(last updated 2026-09-29)

- Auto-pause delay for General Purpose: **minimum 15 minutes, default 60**,
  maximum 7 days. Paused = compute cost zero, storage still billed.
  Default min vCores 0.5.

**[Serverless billing](https://learn.microsoft.com/en-us/azure/azure-sql/database/serverless-tier-billing)**
(last updated 2026-07-29)

- Billed per second: `max(min vCores, vCores used, min memory GB / 3,
  memory used GB / 3)`. While idle but not yet paused you pay the minimum.
- Worked example uses **$0.000145 per vCore-second** (the reference rate
  in `sqlvec cost`, labelled "verify").

**[vCore resource limits, single databases](https://learn.microsoft.com/en-us/azure/azure-sql/database/resource-limits-vcore-single-databases)**

- GP serverless Gen5 min memory: GP_S_Gen5_1 **2.02 GB**, GP_S_Gen5_2
  **2.05 GB**, GP_S_Gen5_4 **2.10 GB** (min vCores 0.5). So at 0.5 min
  vCores the billing floor is set by memory: 2.05 / 3 = 0.683 vCores.

## SQL Server 2025 on Linux / containers

**[Quickstart: install SQL Server on Ubuntu](https://learn.microsoft.com/en-us/sql/linux/quickstart-install-connect-ubuntu)**

- At least 2 GB of RAM. The repo for SQL Server 2025 is
  `https://packages.microsoft.com/config/ubuntu/<version>/mssql-server-2025.list`
  (documented for 22.04; Ubuntu 24.04 is listed as preview for 2025).

**Container tags** (from [releasealert.dev's mirror of the MCR tag list](https://releasealert.dev/mcr/mssql%2Fserver)):
`2025-latest` was updated 2026-08-28, alongside `2025-CU9-ubuntu-24.04` and
`2025-CU9-ubuntu-22.04`. I couldn't reach mcr.microsoft.com from the
sandbox to confirm which Ubuntu `2025-latest` is built on.

**[SQL Server 2025 on Ubuntu 24.04 LTS](https://ubuntu.com/blog/sql-server-2025-ubuntu-24-04-lts)**
(Canonical, 2026-02-06): GA on 24.04 starting with CU1.

## Python driver

**[mssql-python on PyPI](https://pypi.org/project/mssql-python)**: 1.15.0
(2026-09-11), Python >= 3.10, manylinux_2_28 / musllinux / macOS / Windows
wheels. Verified locally: on Ubuntu 24.04 `pip install mssql-python` pulls
in `mssql-python-odbc` with Microsoft ODBC Driver 18.6 libraries for
Debian/Ubuntu, and imports and attempts connections with **no** system
`msodbcsql18` or unixODBC install. It accepts `?` (qmark) and `%(name)s`
parameters. Its docs don't mention the vector type, which is why sqlvec
uses JSON text for vectors.

[pyodbc](https://pypi.org/project/pyodbc/) would also work with the same SQL
(qmark parameters), but needs unixODBC and msodbcsql18 installed from
packages.microsoft.com. I haven't tested sqlvec with it.
