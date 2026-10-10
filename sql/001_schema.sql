-- sqlvec schema: documents, chunks with a native VECTOR column, a metadata
-- filter helper, and a small key/value table that remembers the settings.
--
-- Runs on SQL Server 2025 (17.x), Azure SQL Database and Azure SQL Managed
-- Instance (MI needs the "SQL Server 2025" or "Always-up-to-date" update
-- policy for vector features). Idempotent: safe to run again.
--
-- __DIMS__ is replaced by sqlvec with a validated integer (1..1998) before
-- the script runs. Nothing else in this file is templated. If you run it by
-- hand in SSMS / sqlcmd, replace __DIMS__ yourself (e.g. with 1536).
--
-- Batches are separated by GO, like any sqlcmd script.

IF OBJECT_ID(N'dbo.sqlvec_meta', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sqlvec_meta
    (
        meta_key   NVARCHAR(100) NOT NULL CONSTRAINT PK_sqlvec_meta PRIMARY KEY,
        meta_value NVARCHAR(400) NOT NULL
    );
END
GO

IF OBJECT_ID(N'dbo.sqlvec_documents', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sqlvec_documents
    (
        id           INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_sqlvec_documents PRIMARY KEY,
        source       NVARCHAR(400) NOT NULL CONSTRAINT UQ_sqlvec_documents_source UNIQUE,
        title        NVARCHAR(400) NOT NULL,
        content_hash CHAR(64)      NOT NULL,
        metadata     NVARCHAR(MAX) NULL
            CONSTRAINT CK_sqlvec_documents_metadata CHECK (metadata IS NULL OR ISJSON(metadata) = 1),
        embedder     NVARCHAR(200) NOT NULL,
        ingested_at  DATETIME2(0)  NOT NULL
            CONSTRAINT DF_sqlvec_documents_ingested_at DEFAULT SYSUTCDATETIME()
    );
END
GO

-- The vector index (DiskANN) needs "a primary key clustered index on an int
-- column", so chunks.id is exactly that. The full-text index uses the same
-- key. Vector columns can't carry DEFAULT / CHECK / key constraints, only
-- NULL / NOT NULL, so validation of the vectors happens client side.
IF OBJECT_ID(N'dbo.sqlvec_chunks', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sqlvec_chunks
    (
        id          INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_sqlvec_chunks PRIMARY KEY CLUSTERED,
        document_id INT NOT NULL
            CONSTRAINT FK_sqlvec_chunks_document REFERENCES dbo.sqlvec_documents (id) ON DELETE CASCADE,
        chunk_index INT NOT NULL,
        content     NVARCHAR(MAX) NOT NULL,
        metadata    NVARCHAR(MAX) NULL
            CONSTRAINT CK_sqlvec_chunks_metadata CHECK (metadata IS NULL OR ISJSON(metadata) = 1),
        embedding   VECTOR(__DIMS__) NOT NULL,
        CONSTRAINT UQ_sqlvec_chunks_doc_chunk UNIQUE (document_id, chunk_index)
    );
END
GO

-- Metadata filter: returns 1 when every key/value pair in @filter (a flat
-- JSON object, e.g. {"team":"platform"}) is present in @metadata.
-- OPENJSON on both sides means no JSON path is ever built from user input.
-- Values compare as text, so {"year":"2026"} matches {"year":2026}.
CREATE OR ALTER FUNCTION dbo.sqlvec_matches_filter
(
    @metadata NVARCHAR(MAX),
    @filter   NVARCHAR(MAX)
)
RETURNS BIT
AS
BEGIN
    IF @filter IS NULL RETURN 1;
    IF EXISTS (
        SELECT 1
        FROM OPENJSON(@filter) AS f
        WHERE NOT EXISTS (
            SELECT 1
            FROM OPENJSON(@metadata) AS m
            WHERE m.[key] = f.[key] AND m.[value] = f.[value]
        )
    )
        RETURN 0;
    RETURN 1;
END
GO
