-- Approximate search with VECTOR_SEARCH, earlier (deprecated) index version
-- syntax using TOP_N. The newer engine rejects TOP_N on a version 3 index
-- (Msg 42274), so sqlvec tries ann_search.sql first and only falls back to
-- this one when that fails.
--
-- With this index version, predicates are applied AFTER the TOP_N nearest
-- neighbours come back, so a selective filter can return fewer than k rows.
-- sqlvec asks for more neighbours (__TOP_N__ = k * oversample) when a filter
-- is set, to soften that.
--
-- Placeholders: __TABLE__, __DIMS__, __K__, __TOP_N__ (validated integers or
-- fixed table names). Parameters: query vector JSON, filter JSON or NULL.
DECLARE @qv VECTOR(__DIMS__) = CAST(? AS VECTOR(__DIMS__));
DECLARE @filter NVARCHAR(MAX) = ?;

SELECT TOP (__K__)
    t.id,
    r.distance
FROM VECTOR_SEARCH(
        TABLE = __TABLE__ AS t,
        COLUMN = embedding,
        SIMILAR_TO = @qv,
        METRIC = 'cosine',
        TOP_N = __TOP_N__
    ) AS r
__FILTER__
ORDER BY r.distance;
