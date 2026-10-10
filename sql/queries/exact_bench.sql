-- Exact top-k by cosine distance on the benchmark table (no index used:
-- VECTOR_DISTANCE is always exact). Same placeholders as ann_search.sql.
DECLARE @qv VECTOR(__DIMS__) = CAST(? AS VECTOR(__DIMS__));

SELECT TOP (__K__)
    t.id,
    VECTOR_DISTANCE('cosine', t.embedding, @qv) AS distance
FROM __TABLE__ AS t
ORDER BY distance, t.id;
