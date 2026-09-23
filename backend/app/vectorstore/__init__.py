"""External vector-store backends for dense retrieval.

Postgres + pgvector remains the system of record (documents, chunk text,
lexical ``search_vector``). A dedicated vector store holds a *dense index
copy*: one point per chunk, keyed by the Postgres chunk UUID, with a payload
mirror of the visibility attributes. Final access control is always enforced
by hydrating candidate IDs from Postgres, so a stale or over-permissive
payload filter can never leak cross-tenant evidence.
"""

from app.vectorstore.base import (
    VectorHit,
    VectorPoint,
    VectorStore,
    VectorStoreDimensionError,
    VectorStoreUnavailableError,
)
from app.vectorstore.qdrant import (
    QdrantVectorStore,
    build_point_id,
    match_any,
    match_value,
    point_payload,
)

__all__ = [
    "QdrantVectorStore",
    "VectorHit",
    "VectorPoint",
    "VectorStore",
    "VectorStoreDimensionError",
    "VectorStoreUnavailableError",
    "build_point_id",
    "match_any",
    "match_value",
    "point_payload",
]
