# Native vectors in SQL Server 2025

SQL Server 2025, Azure SQL Database and Azure SQL Managed Instance have a
built-in VECTOR data type. A column is declared as VECTOR(1536) and stores
single-precision floats in a binary format. Clients see a JSON array such as
[0.1, 2, 30], and the engine converts in both directions.

A float32 vector can have up to 1,998 dimensions. Half precision (float16)
doubles that to 3,996 and halves the storage; it is generally available in
Azure SQL and still a preview feature in SQL Server 2025.

VECTOR_DISTANCE computes cosine, euclidean or dot distance between two
vectors. It is always exact and never uses an index, which is fine for tens
of thousands of rows. Microsoft's guidance is that exact search is the right
choice up to roughly fifty thousand vectors, and beyond that when other
filters narrow the candidates.

There is no extension to install and no second database to keep in sync.
The vectors live in the same row as the text, so backups, point-in-time
restore, row-level security and transactions cover them too.
