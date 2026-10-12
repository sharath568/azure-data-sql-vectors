## sqlvec bench: 50,000 x 1536 float32, 100 queries, k=10

- Engine: Azure SQL Database 12.0.2000.8
- Server CPUs / memory: 2 / 911.9 GiB
- Client: macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.14.8, mssql-python 1.15.0
- Data: 64 clusters, noise 1.0, seed 42
- Load time: 183.90 s
- DiskANN build time: 126.04 s (index version 3)

| search | recall@k | p50 ms | p95 ms | mean ms |
|---|---:|---:|---:|---:|
| exact (VECTOR_DISTANCE) | 1.000 | 188.06 | 228.54 | 193.84 |
| DiskANN (VECTOR_SEARCH) | 0.705 | 154.54 | 444.13 | 205.69 |

| sp_spaceused | rows | reserved | data | index |
|---|---:|---:|---:|---:|
| before index | 50,000 | 392.1 MiB | 390.6 MiB | 1.5 MiB |
| after index | 50,000 | 426.8 MiB | 390.6 MiB | 35.9 MiB |

ANN query form: SELECT TOP (k) WITH APPROXIMATE ... VECTOR_SEARCH.

Notes:

- sp_spaceused is reported as-is. I haven't confirmed from the docs whether it counts the DiskANN graph's internal storage, so compare before/after with care.

Latency is client-side wall time per query (execute + fetch), one query at a time.
