# DiskANN vector indexes

For approximate search SQL Server uses DiskANN, a graph index designed to
work well even when the graph doesn't fit in memory. You create it with
CREATE VECTOR INDEX ... WITH (METRIC = 'cosine', TYPE = 'DiskANN') and query
it with the VECTOR_SEARCH function and SELECT TOP (10) WITH APPROXIMATE.

Rules worth knowing before you try it:

- The table needs a clustered primary key on an INT column.
- The table needs at least 100 rows with vectors, or creation fails.
- You can't TRUNCATE a table that has a vector index.
- The metric in VECTOR_SEARCH must match the metric of the index.

The vector index is generally available in Azure SQL Database. In SQL
Server 2025 it is a preview feature, and you have to turn on the
PREVIEW_FEATURES database scoped configuration first.

Older builds of the index made the table read-only: no INSERT, UPDATE or
DELETE until you dropped it. The current version maintains the index during
normal DML and applies WHERE filters while it searches instead of after.
