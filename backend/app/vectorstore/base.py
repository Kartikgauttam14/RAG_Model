"""Backend-neutral contract for dedicated dense vector stores.

The protocol is deliberately narrow so pgvector (SQL, in HybridRetriever),
Qdrant, or a future Milvus adapter can sit behind it. Filters are expressed
in neutral terms (tenant, access scopes, exact-match attributes) and each
adapter translates them into its native filter language.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorPoint:
    """One dense-index entry. ``point_id`` is the Postgres chunk UUID as a string."""

    point_id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorHit:
    """One scored candidate returned by a vector search."""

    point_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStoreUnavailableError(RuntimeError):
    """The vector store could not be reached or answered with an error."""


class VectorStoreDimensionError(RuntimeError):
    """The existing collection was built for a different embedding dimension."""


class VectorStore(Protocol):
    """Minimal surface the retrieval and ingestion pipeline needs."""

    @property
    def backend_name(self) -> str: ...

    async def ensure_collection(self) -> None:
        """Create the collection (and payload indexes) when missing; validate dimension."""
        ...

    async def upsert(self, points: list[VectorPoint]) -> None:
        """Insert or replace points keyed by ``point_id``. Empty input is a no-op."""
        ...

    async def delete_by_version(self, *, document_version_id: str) -> None:
        """Remove every point belonging to one Postgres document version."""
        ...

    async def search(
        self,
        query_vector: list[float],
        *,
        limit: int,
        tenant_id: str,
        scopes: list[str],
        extra_match: dict[str, str | list[str]] | None = None,
    ) -> list[VectorHit]:
        """Nearest neighbours pre-filtered to ``tenant_id`` and one of ``scopes``."""
        ...
