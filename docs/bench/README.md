# Benchmark results

Only real, measured runs go in this folder, each as the `.md` + `.json` pair
that `sqlvec bench --out` writes, with a note on where it ran.

## Azure SQL Database, free offer (2026-10-11)

Serverless `GP_S_Gen5_2` (free offer, 0.5 to 2 vCores), Central US, queried
from a laptop over the internet, one query at a time.

| run | exact p50 | DiskANN p50 | DiskANN recall@10 | DiskANN build |
|---|---:|---:|---:|---:|
| [5k x 384](azure-sql-free-5k-384.md) | 95 ms | 116 ms | 0.999 | 15 s |
| [50k x 1536](azure-sql-free-50k-1536.md) | 188 ms | 155 ms | **0.705** | 126 s |

What I take from it:

- **At 50k vectors, exact search was the better choice.** It returned the true
  top 10 every time in 188 ms. DiskANN was a little faster at the median, slower
  at p95 (444 ms vs 229 ms), and found only 70% of the true neighbours. Most of
  both latencies is the round trip from my laptop, so the server-side gap is
  bigger than it looks, but the recall gap isn't.
- **Recall was the surprise.** On this synthetic, clustered data DiskANN on
  Azure SQL lost almost a third of the true neighbours at 50k x 1536.
  sqlvec's bench uses the index with its defaults and doesn't tune anything,
  so this is the out-of-the-box result. Measure recall on your own
  embeddings before you switch from exact search; the "exact search up to
  ~50k vectors" guidance held up here.
- **Server memory: ignore it.** `sys.dm_os_sys_info` reports the host's
  memory (911.9 GiB), not what the serverless database can use.

One run each, synthetic data. Treat the numbers as observations.

The CI job also runs `sqlvec bench -n 5000 --dims 384` against the
`mssql/server:2025-latest` service container on every push and uploads the
report as the `bench-ci-5k-384` artifact. Those numbers describe a shared
GitHub-hosted runner.

To add a run:

```bash
sqlvec bench -n 50000 --dims 1536 --out docs/bench/<where>-50k-1536
```

and say what `<where>` was: SQL Server CU and edition, container or VM size,
or Azure SQL SKU and region.
