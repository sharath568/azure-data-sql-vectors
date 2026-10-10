-- Exact (k-nearest-neighbour) vector search.
--
-- VECTOR_DISTANCE is always exact and never uses a vector index, even if
-- one exists (that's what VECTOR_SEARCH is for). Microsoft's guidance is
-- that exact search is fine up to roughly 50,000 vectors, or more when other
-- predicates cut the candidates down to that range. For a lot of RAG
-- corpora that's the whole story: no index, perfect recall.
--
-- The query vector arrives as JSON text (that's how the Python drivers send
-- it) and is converted once into a VECTOR variable.
-- Distance is cosine distance: 0 = same direction, 2 = opposite.
--
-- __DIMS__ is replaced with a validated integer before the script runs.

CREATE OR ALTER PROCEDURE dbo.sqlvec_search_exact
    @query_vector NVARCHAR(MAX),
    @k            INT = 5,
    @filter       NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF @k IS NULL OR @k < 1 OR @k > 1000
        THROW 50001, N'@k must be between 1 and 1000', 1;

    DECLARE @qv VECTOR(__DIMS__) = CAST(@query_vector AS VECTOR(__DIMS__));

    SELECT TOP (@k)
        c.id,
        d.source,
        c.chunk_index,
        c.content,
        VECTOR_DISTANCE('cosine', c.embedding, @qv) AS distance
    FROM dbo.sqlvec_chunks AS c
    JOIN dbo.sqlvec_documents AS d ON d.id = c.document_id
    WHERE dbo.sqlvec_matches_filter(c.metadata, @filter) = 1
    ORDER BY distance, c.id;
END
GO
