-- Keyword and hybrid search. Needs the full-text index from 002_fulltext.sql;
-- sqlvec only runs this script when that index exists, because a procedure
-- that references FREETEXTTABLE on a table without a full-text index won't
-- work.
--
-- Hybrid = Reciprocal Rank Fusion (RRF) of two ranked lists:
--   1. vector candidates: exact VECTOR_DISTANCE (cosine), best @candidates
--   2. keyword candidates: FREETEXTTABLE rank, best @candidates
-- score(chunk) = sum over lists of 1 / (@rrf_k + rank in that list)
--
-- RRF only looks at ranks, so the cosine distance and the full-text RANK
-- never have to be put on the same scale. src/sqlvec/rrf.py is the same
-- maths in Python, and an integration test checks both agree.
--
-- FREETEXTTABLE takes plain text (no query syntax to escape), matches any
-- of the words, and applies stemming and the system stoplist. That's what
-- you want for a natural-language question. (The PostgreSQL version of this
-- project learned the hard way that "all words must match" makes the
-- keyword side come back empty for real questions.)
--
-- Both sides apply the same metadata filter.
-- __DIMS__ is replaced with a validated integer before the script runs.

CREATE OR ALTER PROCEDURE dbo.sqlvec_search_fts
    @query_text NVARCHAR(4000),
    @k          INT = 5,
    @filter     NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF @k IS NULL OR @k < 1 OR @k > 1000
        THROW 50001, N'@k must be between 1 and 1000', 1;
    IF @query_text IS NULL OR LEN(LTRIM(RTRIM(@query_text))) = 0
        THROW 50002, N'@query_text must not be empty', 1;

    SELECT TOP (@k)
        c.id,
        d.source,
        c.chunk_index,
        c.content,
        CAST(ft.[RANK] AS FLOAT) AS fts_rank_score
    FROM FREETEXTTABLE(dbo.sqlvec_chunks, content, @query_text) AS ft
    JOIN dbo.sqlvec_chunks AS c ON c.id = ft.[KEY]
    JOIN dbo.sqlvec_documents AS d ON d.id = c.document_id
    WHERE dbo.sqlvec_matches_filter(c.metadata, @filter) = 1
    ORDER BY ft.[RANK] DESC, c.id;
END
GO

CREATE OR ALTER PROCEDURE dbo.sqlvec_search_hybrid
    @query_text   NVARCHAR(4000),
    @query_vector NVARCHAR(MAX),
    @k            INT = 5,
    @filter       NVARCHAR(MAX) = NULL,
    @candidates   INT = 50,
    @rrf_k        INT = 60
AS
BEGIN
    SET NOCOUNT ON;

    IF @k IS NULL OR @k < 1 OR @k > 1000
        THROW 50001, N'@k must be between 1 and 1000', 1;
    IF @candidates IS NULL OR @candidates < 1 OR @candidates > 10000
        THROW 50003, N'@candidates must be between 1 and 10000', 1;
    IF @rrf_k IS NULL OR @rrf_k < 0
        THROW 50004, N'@rrf_k must be >= 0', 1;
    IF @query_text IS NULL OR LEN(LTRIM(RTRIM(@query_text))) = 0
        THROW 50002, N'@query_text must not be empty', 1;

    DECLARE @qv VECTOR(__DIMS__) = CAST(@query_vector AS VECTOR(__DIMS__));

    WITH vec AS (
        SELECT TOP (@candidates)
            c.id,
            ROW_NUMBER() OVER (ORDER BY VECTOR_DISTANCE('cosine', c.embedding, @qv), c.id) AS rnk
        FROM dbo.sqlvec_chunks AS c
        WHERE dbo.sqlvec_matches_filter(c.metadata, @filter) = 1
        ORDER BY rnk
    ),
    kw AS (
        SELECT TOP (@candidates)
            c.id,
            ROW_NUMBER() OVER (ORDER BY ft.[RANK] DESC, c.id) AS rnk
        FROM FREETEXTTABLE(dbo.sqlvec_chunks, content, @query_text) AS ft
        JOIN dbo.sqlvec_chunks AS c ON c.id = ft.[KEY]
        WHERE dbo.sqlvec_matches_filter(c.metadata, @filter) = 1
        ORDER BY rnk
    ),
    fused AS (
        SELECT
            COALESCE(v.id, f.id) AS id,
            v.rnk AS vector_rank,
            f.rnk AS fts_rank,
            -- FLOAT, not DECIMAL: 1.0 / 61 in DECIMAL loses digits and
            -- changes tie-breaks compared with the Python reference.
            COALESCE(CAST(1 AS FLOAT) / (@rrf_k + v.rnk), 0)
              + COALESCE(CAST(1 AS FLOAT) / (@rrf_k + f.rnk), 0) AS score
        FROM vec AS v
        FULL OUTER JOIN kw AS f ON f.id = v.id
    )
    SELECT TOP (@k)
        fu.id,
        d.source,
        c.chunk_index,
        c.content,
        fu.score,
        fu.vector_rank,
        fu.fts_rank
    FROM fused AS fu
    JOIN dbo.sqlvec_chunks AS c ON c.id = fu.id
    JOIN dbo.sqlvec_documents AS d ON d.id = c.document_id
    ORDER BY fu.score DESC, fu.id;
END
GO
