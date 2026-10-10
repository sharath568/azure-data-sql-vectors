-- Full-text catalog and index for the keyword half of hybrid search.
--
-- Full-text search is available in Azure SQL Database and Managed Instance.
-- On SQL Server it is a separate component, and on Linux that means the
-- mssql-server-fts package. The official mcr.microsoft.com/mssql/server
-- container images don't include it (see docker/fts/Dockerfile for an image
-- that does). So this script checks FULLTEXTSERVICEPROPERTY first and does
-- nothing when full-text isn't installed; sqlvec then reports that hybrid
-- and fts search modes are unavailable instead of failing.
--
-- The DDL runs through EXEC so the batch still compiles on an engine
-- without full-text. All strings are fixed; nothing is user input.

IF FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') = 1
   AND NOT EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name = N'sqlvec_ftcat')
BEGIN
    EXEC (N'CREATE FULLTEXT CATALOG sqlvec_ftcat;');
END
GO

IF FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') = 1
   AND NOT EXISTS (
       SELECT 1 FROM sys.fulltext_indexes WHERE object_id = OBJECT_ID(N'dbo.sqlvec_chunks')
   )
BEGIN
    -- LANGUAGE 1033 = English word breaker and stemmer.
    -- CHANGE_TRACKING AUTO keeps the index current after inserts, but
    -- population is asynchronous: a chunk can be searchable by vector a
    -- moment before it is searchable by keyword.
    EXEC (N'CREATE FULLTEXT INDEX ON dbo.sqlvec_chunks (content LANGUAGE 1033)
            KEY INDEX PK_sqlvec_chunks ON sqlvec_ftcat
            WITH (CHANGE_TRACKING = AUTO, STOPLIST = SYSTEM);');
END
GO
