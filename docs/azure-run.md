# First Azure run (2026-10-11)

Everything in this repo had only run against SQL Server 2025 in Docker. This
is the first end-to-end run on Azure, done from a laptop with the scripts in
this repo.

## Setup

| | |
|---|---|
| Database | Azure SQL Database free offer, serverless `GP_S_Gen5_2`, Central US, `useFreeLimit: true`, `AutoPause` on exhaustion, auto-pause after 60 min |
| Embeddings | Azure OpenAI `text-embedding-3-small` (1536 dims), `Standard`, East US |
| Deploy | `LOCATION=centralus OPENAI_LOCATION=eastus DEPLOY_OPENAI=true ./scripts/up.sh` |
| Client | macOS, Python 3.14, mssql-python 1.15.0 |

## What ran

| Step | Result |
|---|---|
| `sqlvec doctor` | Azure SQL Database 12.0.2000.8, `VECTOR` type yes, full-text yes |
| `sqlvec init` | all migrations applied, full-text index created |
| `sqlvec ingest examples/docs` | 5 files, 9 chunks, ~1,520 tokens embedded with Azure OpenAI |
| `sqlvec search` exact / fts / hybrid | all three return the free-offer doc first for "how do I keep the database free?" |
| `sqlvec index` | skipped on purpose: 9 chunks is under the 100-row minimum |
| `sqlvec bench` 5k x 384 and 50k x 1536 | see [bench/README.md](bench/README.md) |
| `sql/azure/in_db_embeddings.sql` | installed after one fix (below); both procedures work |

## In-database embeddings

With the external model in place, the database calls Azure OpenAI itself:

| Call | Time from the client | Result |
|---|---:|---|
| `EXEC dbo.sqlvec_search_hybrid_text N'how do I keep the database free?', 3` | 323 ms | same top result as the Python path (vector #1, full-text #1) |
| `EXEC dbo.sqlvec_ingest_text` on the first 6,000 characters of this README | 950 ms | 6 chunks, each 999 to 1,000 characters |

Two things worth knowing:

1. **`AI_GENERATE_CHUNKS` with `CHUNK_TYPE = FIXED` cuts mid-word.** One
   chunk started with "ird project in a series". sqlvec's Python chunker
   prefers paragraph and sentence boundaries. For retrieval quality the
   Python path is better; the T-SQL path wins on simplicity (no app code).
2. **Full-text lags behind.** Right after the T-SQL ingest, the new chunks
   showed up in the vector half of hybrid search but not in the full-text
   half (`fts_rank` NULL), because full-text population is asynchronous.
   The same thing happened after `sqlvec ingest`: a `--mode fts` search right
   after ingest returned nothing, and a few seconds later it worked.

## What the run caught

| Problem | Fix |
|---|---|
| `in_db_embeddings.sql` failed on Azure SQL: "Statement 'CONFIG' is not supported in this version of SQL Server". Azure SQL rejects `RECONFIGURE` at compile time, even inside an `IF` branch that doesn't run there | `EXEC (N'RECONFIGURE WITH OVERRIDE')`, so it is only compiled on SQL Server |
| `AUTOPAUSE_DELAY=15` failed: "Only default value for auto pause delay is allowed for Free Limit database with auto pause exhaustion behavior" | documented in [infra/README.md](../infra/README.md); the default (60) stays |
| Azure OpenAI preflight failed with `InsufficientQuota` | the Free Trial allows one Azure OpenAI account per region and another project had Central US; `OPENAI_LOCATION=eastus` |
| No way to choose the embedding SKU from `up.sh` | new `EMBED_SKU` variable |
| macOS: "OpenSSL library could not be loaded" | `DYLD_LIBRARY_PATH=/opt/homebrew/opt/openssl@3/lib` and `python -m sqlvec` (new `__main__.py`); see infra/README.md |

## Cost

The database is on the free offer, so its compute came out of the monthly
100,000 vCore-second allowance, not the bill. The 50k x 1536 bench was the
heaviest part: about 5 minutes of load and index build. Embedding about
1,500 tokens plus a few queries with `text-embedding-3-small` costs a
fraction of a cent. The resource group was deleted the same day.
