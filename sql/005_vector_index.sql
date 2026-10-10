-- Approximate nearest neighbour (ANN) index: DiskANN.
--
-- Status as of October 2026 (Microsoft Learn, CREATE VECTOR INDEX page,
-- last updated 2026-09-29):
--   * GA in Azure SQL Database, SQL database in Fabric, and Azure SQL
--     Managed Instance with the Always-up-to-date update policy.
--   * PREVIEW in SQL Server 2025 (17.x) and in Managed Instance with the
--     SQL Server 2025 update policy. There you must first run:
--         ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON;
--     (sqlvec does that for you on SQL Server, see db.enable_preview_features.)
--
-- Requirements and limits that matter here:
--   * the table needs a clustered primary key on an INT column (we have it)
--   * at least 100 rows with non-NULL vectors, or creation fails (Msg 42266);
--     sqlvec checks the row count before running this
--   * the metric must match the one used in VECTOR_SEARCH (cosine here)
--   * no TRUNCATE TABLE while the index exists; it isn't partitioned and
--     isn't replicated to subscribers
--   * DML: with the latest index version (build_parameters $.Version = 3)
--     INSERT/UPDATE/DELETE/MERGE work and the index is maintained. With the
--     earlier version the table becomes READ-ONLY after the index is built
--     (or you opt into stale results with ALLOW_STALE_VECTOR_INDEX). Which
--     version a given SQL Server 2025 CU builds isn't stated in the docs I
--     read; `sqlvec doctor` prints it so you can see for yourself.

-- sys.vector_indexes inherits its columns from sys.indexes.
IF NOT EXISTS (
    SELECT 1
    FROM sys.vector_indexes
    WHERE object_id = OBJECT_ID(N'dbo.sqlvec_chunks')
      AND name = N'vix_sqlvec_chunks_embedding'
)
BEGIN
    CREATE VECTOR INDEX vix_sqlvec_chunks_embedding
        ON dbo.sqlvec_chunks (embedding)
        WITH (METRIC = 'cosine', TYPE = 'DiskANN');
END
GO
