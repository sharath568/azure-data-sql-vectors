## sqlvec bench: 5,000 x 384 float32, 100 queries, k=10

- Engine: Azure SQL Database 12.0.2000.8
- Server CPUs / memory: 2 / 911.9 GiB
- Client: macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.14.8, mssql-python 1.15.0
- Data: 64 clusters, noise 1.0, seed 42
- Load time: 7.15 s
- DiskANN build time: 15.43 s (index version 3)

| search | recall@k | p50 ms | p95 ms | mean ms |
|---|---:|---:|---:|---:|
| exact (VECTOR_DISTANCE) | 1.000 | 95.14 | 181.64 | 110.02 |
| DiskANN (VECTOR_SEARCH) | 0.999 | 116.05 | 153.64 | 126.03 |

| sp_spaceused | rows | reserved | data | index |
|---|---:|---:|---:|---:|
| before index | 5,000 | 7.9 MiB | 7.8 MiB | 40 KiB |
| after index | 5,000 | 10.6 MiB | 7.8 MiB | 2.6 MiB |

ANN query form: SELECT TOP (k) WITH APPROXIMATE ... VECTOR_SEARCH.

Notes:

- sp_spaceused is reported as-is. I haven't confirmed from the docs whether it counts the DiskANN graph's internal storage, so compare before/after with care.

Latency is client-side wall time per query (execute + fetch), one query at a time.
