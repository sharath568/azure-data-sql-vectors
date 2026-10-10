-- OPTIONAL: let the database call Azure OpenAI itself.
--
-- Azure SQL Database / Managed Instance / SQL Server 2025 only. sqlvec never
-- runs this script automatically. It needs an Azure OpenAI resource with an
-- embeddings deployment, and it stores the API key in a database scoped
-- credential (encrypted by the database master key). For anything beyond a
-- demo, use a managed identity instead of a key (see the Microsoft Learn
-- page for CREATE EXTERNAL MODEL).
--
-- Status (Microsoft Learn + Azure SQL dev blog, checked 2026-10-10):
--   * AI_GENERATE_EMBEDDINGS and CREATE EXTERNAL MODEL: announced GA on
--     2026-05-08 for Azure SQL Database, Azure SQL Managed Instance and
--     SQL Server 2025.
--   * AI_GENERATE_CHUNKS: listed for SQL Server 2025, Azure SQL Database and
--     SQL database in Fabric (not Managed Instance). I could not find a clear
--     GA/preview statement for it. If it errors on SQL Server 2025, try
--     ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON first.
--
-- Run with sqlcmd variables, for example:
--   sqlcmd -S <server>.database.windows.net -d sqlvec -U <user> -P <pwd> \
--     -v endpoint="https://<name>.openai.azure.com/" \
--        api_key="<key>" deployment="text-embedding-3-small" dims="1536" \
--        master_key_password="<a strong password>" \
--     -i sql/azure/in_db_embeddings.sql
--
-- endpoint must end with a slash. dims must match the chunks.embedding
-- column (sqlvec init created it with SQLVEC_EMBED_DIMS).

-- 1. Outbound REST calls. Enabled by default on Azure SQL Database (and
--    Fabric); must be switched on for SQL Server 2025 and Managed Instance.
IF CAST(SERVERPROPERTY('EngineEdition') AS INT) <> 5
BEGIN
    EXEC sp_configure 'external rest endpoint enabled', 1;
    RECONFIGURE WITH OVERRIDE;
END
GO

-- 2. A master key to encrypt the credential.
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = N'$(master_key_password)';
GO

-- 3. The credential. Its name must be the endpoint URL.
IF EXISTS (SELECT 1 FROM sys.database_scoped_credentials WHERE name = N'$(endpoint)')
    DROP DATABASE SCOPED CREDENTIAL [$(endpoint)];
GO
CREATE DATABASE SCOPED CREDENTIAL [$(endpoint)]
    WITH IDENTITY = 'HTTPEndpointHeaders', SECRET = '{"api-key":"$(api_key)"}';
GO

-- 4. The external model. (sys.external_models is the catalog view I expect
--    from the CREATE/DROP EXTERNAL MODEL docs; if it isn't there on your
--    build, drop the model by hand before re-running.)
IF EXISTS (SELECT 1 FROM sys.external_models WHERE name = N'sqlvec_embeddings')
    DROP EXTERNAL MODEL sqlvec_embeddings;
GO
CREATE EXTERNAL MODEL sqlvec_embeddings
WITH (
    LOCATION   = '$(endpoint)openai/deployments/$(deployment)/embeddings?api-version=2024-10-21',
    API_FORMAT = 'Azure OpenAI',
    MODEL_TYPE = EMBEDDINGS,
    MODEL      = '$(deployment)',
    CREDENTIAL = [$(endpoint)]
);
GO

-- 5. Hybrid search where the database embeds the question itself:
--    one round trip from the app, no embedding code in the client.
CREATE OR ALTER PROCEDURE dbo.sqlvec_search_hybrid_text
    @query_text NVARCHAR(4000),
    @k          INT = 5,
    @filter     NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @v VECTOR($(dims)) = AI_GENERATE_EMBEDDINGS(@query_text USE MODEL sqlvec_embeddings);
    DECLARE @qv NVARCHAR(MAX) = CAST(@v AS NVARCHAR(MAX));
    EXEC dbo.sqlvec_search_hybrid
        @query_text = @query_text, @query_vector = @qv, @k = @k, @filter = @filter;
END
GO

-- 6. Ingest a document entirely in T-SQL: chunk with AI_GENERATE_CHUNKS,
--    embed with AI_GENERATE_EMBEDDINGS. Fixed-size chunks only (that's the
--    one chunk type documented). Compare with sqlvec's Python chunker, which
--    prefers paragraph and sentence boundaries.
CREATE OR ALTER PROCEDURE dbo.sqlvec_ingest_text
    @source     NVARCHAR(400),
    @title      NVARCHAR(400),
    @text       NVARCHAR(MAX),
    @chunk_size INT = 1000
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;

    DELETE FROM dbo.sqlvec_documents WHERE source = @source;

    DECLARE @doc TABLE (id INT);
    INSERT INTO dbo.sqlvec_documents (source, title, content_hash, metadata, embedder)
    OUTPUT INSERTED.id INTO @doc (id)
    VALUES (
        @source, @title,
        -- SHA-256 of the UTF-16 text, so it won't equal the hash the Python
        -- ingester stores (UTF-8). It only has to be stable for this path.
        LOWER(CONVERT(CHAR(64), HASHBYTES('SHA2_256', CAST(@text AS VARBINARY(MAX))), 2)),
        N'{"ingested_by":"t-sql"}',
        N'external-model:sqlvec_embeddings'
    );

    INSERT INTO dbo.sqlvec_chunks (document_id, chunk_index, content, metadata, embedding)
    SELECT
        d.id,
        c.chunk_order,
        c.chunk,
        N'{"ingested_by":"t-sql"}',
        AI_GENERATE_EMBEDDINGS(c.chunk USE MODEL sqlvec_embeddings)
    FROM @doc AS d
    CROSS APPLY AI_GENERATE_CHUNKS(
        SOURCE = @text,
        CHUNK_TYPE = FIXED,
        CHUNK_SIZE = @chunk_size
    ) AS c;

    COMMIT TRANSACTION;
END
GO

-- Try it:
--   EXEC dbo.sqlvec_ingest_text N'manual/hello.md', N'Hello', N'Some long text ...';
--   EXEC dbo.sqlvec_search_hybrid_text N'how do I keep costs low?', 3;
