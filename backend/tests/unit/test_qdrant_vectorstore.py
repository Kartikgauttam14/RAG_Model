import json

import httpx
import pytest
import respx
from app.config import Settings
from app.vectorstore import (
    QdrantVectorStore,
    VectorPoint,
    VectorStoreDimensionError,
    VectorStoreUnavailableError,
    build_point_id,
    match_any,
    match_value,
    point_payload,
)

BASE = "http://qdrant:6333"
COLLECTION = "mansam_chunks"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "qdrant_url": BASE,
        "qdrant_collection": COLLECTION,
        "embedding_dimension": 3,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_build_point_id_passes_uuid_through_and_maps_other_ids() -> None:
    chunk = "12345678-1234-5678-1234-567812345678"

    assert build_point_id(chunk) == chunk
    assert build_point_id("row-7") == build_point_id("row-7")
    assert build_point_id("row-7") != build_point_id("row-8")


def test_point_payload_carries_filters_but_never_text() -> None:
    payload = point_payload(
        tenant_id="default",
        access_scope="tenant",
        document_id="doc-1",
        document_version_id="ver-1",
        chunk_index=3,
        language="ar",
        section="Delivery",
        page_number=2,
        embedding_model="bge-m3",
    )

    assert payload["tenant_id"] == "default"
    assert payload["access_scope"] == "tenant"
    assert payload["chunk_index"] == 3
    assert "content" not in payload
    assert "text" not in payload
    assert "vector" not in payload


def test_match_helpers_use_qdrant_filter_shapes() -> None:
    assert match_value("tenant_id", "default") == {"key": "tenant_id", "match": {"value": "default"}}

    assert match_value("tenant_id", "default") == {"key": "tenant_id", "match": {"value": "default"}}
    assert match_any("access_scope", ["public", "tenant"]) == {
        "key": "access_scope",
        "match": {"any": ["public", "tenant"]},
    }


@pytest.mark.asyncio
@respx.mock
async def test_ensure_collection_creates_with_hnsw_config_when_missing() -> None:
    respx.get(f"{BASE}/collections/{COLLECTION}").mock(return_value=httpx.Response(404, json={}))
    created = respx.put(f"{BASE}/collections/{COLLECTION}").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/collections/{COLLECTION}/index").mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        await QdrantVectorStore(_settings(), client).ensure_collection()

    body = created.calls[0].request.content.decode().replace(" ", "")
    assert '"size":3' in body
    assert "Cosine" in body
    assert "hnsw_config" in body


@pytest.mark.asyncio
@respx.mock
async def test_ensure_collection_rejects_dimension_mismatch() -> None:
    respx.get(f"{BASE}/collections/{COLLECTION}").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}}}},
        )
    )

    async with httpx.AsyncClient() as client:
        try:
            await QdrantVectorStore(_settings(), client).ensure_collection()
        except VectorStoreDimensionError:
            return
    raise AssertionError("expected VectorStoreDimensionError")


@pytest.mark.asyncio
@respx.mock
async def test_upsert_sends_chunk_uuid_point_ids_with_payload() -> None:
    route = respx.put(f"{BASE}/collections/{COLLECTION}/points?wait=true").mock(
        return_value=httpx.Response(200, json={})
    )
    chunk_id = "12345678-1234-5678-1234-567812345678"

    async with httpx.AsyncClient() as client:
        await QdrantVectorStore(_settings(), client).upsert(
            [VectorPoint(point_id=chunk_id, vector=[0.1, 0.2, 0.3], payload={"tenant_id": "default"})]
        )

    sent = json.loads(route.calls[0].request.content)
    assert sent["points"][0]["id"] == chunk_id
    assert sent["points"][0]["vector"] == [0.1, 0.2, 0.3]
    assert sent["points"][0]["payload"] == {"tenant_id": "default"}


@pytest.mark.asyncio
@respx.mock
async def test_upsert_is_noop_for_empty_input() -> None:
    route = respx.put(f"{BASE}/collections/{COLLECTION}/points?wait=true").mock(
        return_value=httpx.Response(200, json={})
    )

    async with httpx.AsyncClient() as client:
        await QdrantVectorStore(_settings(), client).upsert([])

    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_search_sends_tenant_and_scope_filter() -> None:
    route = respx.post(f"{BASE}/collections/{COLLECTION}/points/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {"id": "12345678-1234-5678-1234-567812345678", "score": 0.91, "payload": {"a": 1}},
                        {"id": "nope", "score": 0.5},
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as client:
        hits = await QdrantVectorStore(_settings(), client).search(
            [0.1, 0.2, 0.3], limit=5, tenant_id="default", scopes=["public", "tenant"]
        )

    sent = json.loads(route.calls[0].request.content)
    assert sent["query"] == [0.1, 0.2, 0.3]
    assert sent["limit"] == 5
    must = sent["filter"]["must"]
    assert {"key": "tenant_id", "match": {"value": "default"}} in must
    assert match_any("access_scope", ["public", "tenant"]) in must
    assert [hit.point_id for hit in hits] == ["12345678-1234-5678-1234-567812345678", "nope"]
    assert hits[0].score == 0.91
    assert hits[0].payload == {"a": 1}


@pytest.mark.asyncio
@respx.mock
async def test_search_raises_unavailable_on_server_error() -> None:
    respx.post(f"{BASE}/collections/{COLLECTION}/points/query").mock(return_value=httpx.Response(500, json={}))

    async with httpx.AsyncClient() as client:
        try:
            await QdrantVectorStore(_settings(), client).search(
                [0.1, 0.2, 0.3], limit=5, tenant_id="default", scopes=["tenant"]
            )
        except VectorStoreUnavailableError:
            return
    raise AssertionError("expected VectorStoreUnavailableError")


@pytest.mark.asyncio
@respx.mock
async def test_delete_by_version_uses_payload_filter() -> None:
    route = respx.post(f"{BASE}/collections/{COLLECTION}/points/delete?wait=true").mock(
        return_value=httpx.Response(200, json={})
    )

    async with httpx.AsyncClient() as client:
        await QdrantVectorStore(_settings(), client).delete_by_version(document_version_id="ver-1")

    sent = json.loads(route.calls[0].request.content)
    assert sent["filter"] == {"must": [{"key": "document_version_id", "match": {"value": "ver-1"}}]}
