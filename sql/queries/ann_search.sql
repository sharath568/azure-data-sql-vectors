-- Approximate search with VECTOR_SEARCH, latest index version syntax:
-- SELECT TOP (N) WITH APPROXIMATE ... ORDER BY distance.
-- WHERE predicates are applied during the search (iterative filtering).
--
-- Placeholders, replaced by sqlvec with validated values before running:
--   __TABLE__  one of a fixed list of sqlvec tables (never user text)
--   __DIMS__   integer 1..1998
--   __K__      integer 1..1000
-- Parameters (bound, never concatenated):
--   1st ?  query vector as JSON text
--   2nd ?  metadata filter as a JSON object, or NULL
-- __FILTER__ is either empty or the metadata predicate below, depending on
-- whether the table has a metadata column (the bench table doesn't).
DECLARE @qv VECTOR(__DIMS__) = CAST(? AS VECTOR(__DIMS__));
DECLARE @filter NVARCHAR(MAX) = ?;

SELECT TOP (__K__) WITH APPROXIMATE
    t.id,
    r.distance
FROM VECTOR_SEARCH(
        TABLE = __TABLE__ AS t,
        COLUMN = embedding,
        SIMILAR_TO = @qv,
        METRIC = 'cosine'
    ) AS r
__FILTER__
ORDER BY r.distance;
