"""Qdrant adapter over the Qdrant REST API (httpx only, no qdrant-client).

Security model: point ids are the Postgres ``document_chunks.id`` UUIDs;
searches always carry tenant + scope filters; Postgres hydration afterwards
re-checks scope and version currency. Chunk text never leaves Postgres.
"""

import uuid

import httpx

from app.config import Settings
from app.monitoring.logging import get_logger
from app.vectorstore.base import (
    VectorHit,
    VectorPoint,
    VectorStoreDimensionError,
    VectorStoreUnavailableError,
)

logger = get_logger(__name__)

_CHUNK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "mansam-rag:document-chunk")


def build_point_id(chunk_id: str, *, namespace: uuid.UUID = _CHUNK_NAMESPACE) -> str:
    """Stable Qdrant point id: UUIDs pass through, other ids map to UUIDv5."""
    try:
        return str(uuid.UUID(str(chunk_id)))
    except ValueError:
        return str(uuid.uuid5(namespace, str(chunk_id)))


def match_value(key: str, value: str) -> dict[str, object]:
    return {"key": key, "match": {"value": value}}


def match_any(key: str, values: list[str]) -> dict[str, object]:
    return {"key": key, "match": {"any": list(values)}}


def point_payload(
    *,
    tenant_id: str,
    access_scope: str,
    document_id: str,
    document_version_id: str,
    chunk_index: int,
    language: str | None = None,
    category: str | None = None,
    section: str | None = None,
    page_number: int | None = None,
    embedding_model: str | None = None,
) -> dict[str, object]:
    """Qdrant payload mirror for one chunk (filter/display attrs, never text)."""
    payload: dict[str, object] = {
        "tenant_id": tenant_id,
        "access_scope": access_scope,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "chunk_index": chunk_index,
    }
    if language:
        payload["language"] = language
    if category:
        payload["category"] = category
    if section:
        payload["section"] = section
    if page_number is not None:
        payload["page_number"] = page_number
    if embedding_model:
        payload["embedding_model"] = embedding_model
    return payload


# __PART2__


class QdrantVectorStore:
    """Qdrant REST adapter implementing the :class:`VectorStore` protocol."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.qdrant_url:
            raise ValueError("QDRANT_URL is required")
        self.settings = settings
        self.base_url = settings.qdrant_url.rstrip("/")
        self.collection = settings.qdrant_collection
        self.client = client or httpx.AsyncClient(timeout=settings.qdrant_timeout_seconds)

    @property
    def backend_name(self) -> str:
        return "qdrant"

    def _headers(self) -> dict[str, str]:
        if self.settings.qdrant_api_key:
            return {"api-key": self.settings.qdrant_api_key}
        return {}

    async def ensure_collection(self) -> None:
        """Create the collection (plus keyword payload indexes) when missing."""
        try:
            response = await self.client.get(
                f"{self.base_url}/collections/{self.collection}",
                headers=self._headers(),
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise VectorStoreUnavailableError(f"Qdrant is unreachable: {exc}") from exc
        if response.status_code == 200:
            self._check_dimension(response.json())
            return
        if response.status_code != 404:
            raise VectorStoreUnavailableError(f"Qdrant collection lookup failed with status {response.status_code}")
        body = {
            "vectors": {
                "size": self.settings.embedding_dimension,
                "distance": "Cosine",
                "hnsw_config": {
                    "m": self.settings.qdrant_hnsw_m,
                    "ef_construct": self.settings.qdrant_hnsw_ef_construct,
                    "full_scan_threshold": self.settings.qdrant_full_scan_threshold,
                },
            },
            "optimizers_config": {"default_segment_number": 2},
        }
        try:
            created = await self.client.put(
                f"{self.base_url}/collections/{self.collection}",
                headers=self._headers(),
                json=body,
                timeout=self.settings.qdrant_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise VectorStoreUnavailableError(f"Qdrant is unreachable: {exc}") from exc
        if created.status_code not in (200, 201):
            raise VectorStoreUnavailableError(f"Qdrant collection creation failed with status {created.status_code}")
        await self._ensure_payload_indexes()

    async def _ensure_payload_indexes(self) -> None:
        for key in ("tenant_id", "access_scope", "document_id", "document_version_id"):
            try:
                await self.client.post(
                    f"{self.base_url}/collections/{self.collection}/index",
                    headers=self._headers(),
                    json={"field_name": key, "field_schema": "keyword"},
                    timeout=self.settings.qdrant_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                logger.warning("qdrant_index_failed", field=key, error_type=type(exc).__name__)

    def _check_dimension(self, body: object) -> None:
        """Refuse to write into a collection built for another embedding size."""
        try:
            assert isinstance(body, dict)
            params = body["result"]["config"]["params"]["vectors"]
            size = params["size"] if isinstance(params, dict) else params[""]["size"]
        except (KeyError, TypeError, AssertionError):
            return
        if int(size) != self.settings.embedding_dimension:
            raise VectorStoreDimensionError(
                f"Qdrant collection '{self.collection}' has size {size}, expected {self.settings.embedding_dimension}"
            )

    # __WRITES__

    async def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        body = {
            "points": [{"id": build_point_id(p.point_id), "vector": p.vector, "payload": p.payload} for p in points]
        }
        try:
            response = await self.client.put(
                f"{self.base_url}/collections/{self.collection}/points?wait=true",
                headers=self._headers(),
                json=body,
                timeout=self.settings.qdrant_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise VectorStoreUnavailableError(f"Qdrant upsert failed: {exc}") from exc
        if response.status_code >= 400:
            raise VectorStoreUnavailableError(f"Qdrant upsert rejected with status {response.status_code}")

    async def delete_by_version(self, *, document_version_id: str) -> None:
        body = {"filter": {"must": [match_value("document_version_id", document_version_id)]}}
        try:
            response = await self.client.post(
                f"{self.base_url}/collections/{self.collection}/points/delete?wait=true",
                headers=self._headers(),
                json=body,
                timeout=self.settings.qdrant_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise VectorStoreUnavailableError(f"Qdrant delete failed: {exc}") from exc
        if response.status_code >= 400:
            raise VectorStoreUnavailableError(f"Qdrant delete failed with status {response.status_code}")

    def _scope_filter(self, scopes: list[str]) -> dict[str, object]:
        if len(scopes) == 1:
            return match_value("access_scope", scopes[0])
        return match_any("access_scope", scopes)

    async def search(
        self,
        query_vector: list[float],
        *,
        limit: int,
        tenant_id: str,
        scopes: list[str],
        extra_match: dict[str, str | list[str]] | None = None,
    ) -> list[VectorHit]:
        must: list[dict[str, object]] = [match_value("tenant_id", tenant_id), self._scope_filter(scopes)]
        for key, value in (extra_match or {}).items():
            must.append(match_value(key, value) if isinstance(value, str) else match_any(key, value))
        body = {
            "query": query_vector,
            "limit": limit,
            "filter": {"must": must},
            "with_payload": True,
            "params": {"hnsw_ef": self.settings.qdrant_search_ef},
        }
        try:
            response = await self.client.post(
                f"{self.base_url}/collections/{self.collection}/points/query",
                headers=self._headers(),
                json=body,
                timeout=self.settings.qdrant_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise VectorStoreUnavailableError(f"Qdrant search failed: {exc}") from exc
        if response.status_code >= 400:
            raise VectorStoreUnavailableError(f"Qdrant search failed with status {response.status_code}")
        result = response.json().get("result") or {}
        hits: list[VectorHit] = []
        for item in result.get("points", []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            payload = item.get("payload")
            hits.append(
                VectorHit(
                    point_id=str(item["id"]),
                    score=float(item.get("score", 0.0)),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        return hits
