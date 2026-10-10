# Notes from an Oracle DBA trying SQL Server vectors

Things that felt familiar: a clustered primary key on an integer id, a
separate full-text catalog, statistics, MAXDOP on index builds, and a
deadlock victim error 1205 when two sessions fight over the same rows.

Things that were new: the vector column can't have CHECK or DEFAULT
constraints, so validation of the embeddings has to happen in the
application. Vectors can't be compared with = or added with +; you use the
vector functions. sp_describe_first_result_set reports a vector column as
varchar, which confuses some tools.

Full-text search on Linux is a separate package called mssql-server-fts,
and the official container image doesn't include it. On Azure SQL Database
it is built in. Population is asynchronous, so a freshly inserted row can
be found by vector search a moment before keyword search sees it.

For metadata filters I would promote any key I filter on often to a real
column and index it, instead of reading JSON on every row.
