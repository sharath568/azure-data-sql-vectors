# Benchmark results

Only real, measured runs go in this folder, each as the `.md` + `.json` pair
that `sqlvec bench --out` writes, with a note on where it ran.

**There are no committed results yet.** I couldn't run SQL Server in the
sandbox I built this in (container registry and Microsoft package hosts
were blocked), so the first numbers come from CI: every push runs
`sqlvec bench -n 5000 --dims 384` against the `mssql/server:2025-latest`
service container and uploads the report as the `bench-ci-5k-384` artifact
(it is also shown in the job summary). Those numbers describe a shared
GitHub-hosted runner, so treat them as "it works and roughly this fast", not
as a performance claim.

To add a run:

```bash
sqlvec bench -n 50000 --dims 1536 --out docs/bench/<where>-50k-1536
```

and say what `<where>` was: SQL Server CU and edition, container or VM size,
or Azure SQL SKU and region.
