# Why hybrid search

Vector search is good at meaning. Ask how to stop paying for an idle
database and it can find a passage about auto-pause even if the words don't
match. Keyword search is good at exact tokens: error numbers like 1205,
setting names like MAXDOP, SKU names like GP_S_Gen5_2. Neither is enough on
its own.

Hybrid search runs both and merges the two ranked lists with Reciprocal
Rank Fusion. Each chunk scores 1 / (60 + rank) for every list it appears
in. A chunk ranked first by vectors and third by keywords scores
1/61 + 1/63. RRF only looks at ranks, so the cosine distance and the
full-text rank never have to be put on the same scale.

In SQL Server the keyword half is full-text search. FREETEXTTABLE takes a
plain question, breaks it into words, applies stemming and the stoplist,
and returns a RANK for every matching row. One stored procedure runs the
vector query, the full-text query and the fusion in a single round trip.
