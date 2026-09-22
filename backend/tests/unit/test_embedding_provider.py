import json

import httpx
import pytest
import respx
from app.config import Settings
from app.embeddings.huggingface import EmbeddingUnavailableError, HuggingFaceEmbeddingProvider

OPENAI_ENDPOINT = "http://127.0.0.1:11434/v1/embeddings"


def _openai_body(vectors: list[list[float]]) -> dict[str, object]:
    return {"data": [{"embedding": vector} for vector in vectors]}


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_endpoint_sends_e5_prefixes_by_default() -> None:
    route = respx.post(OPENAI_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_openai_body([[0.0] * 1024]))
    )
    settings = Settings(embedding_inference_url=OPENAI_ENDPOINT, embedding_dimension=1024)

    async with httpx.AsyncClient() as client:
        await HuggingFaceEmbeddingProvider(settings, client).embed_query("hello")

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == settings.embedding_model
    assert sent["input"] == ["query: hello"]


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_endpoint_honours_configured_prefixes() -> None:
    route = respx.post(OPENAI_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=_openai_body([[0.0] * 1024, [0.0] * 1024])),
            httpx.Response(200, json=_openai_body([[0.0] * 1024])),
        ]
    )
    settings = Settings(
        embedding_inference_url=OPENAI_ENDPOINT,
        embedding_dimension=1024,
        embedding_model="bge-m3",
        embedding_query_prefix="",
        embedding_passage_prefix="",
    )

    async with httpx.AsyncClient() as client:
        provider = HuggingFaceEmbeddingProvider(settings, client)
        await provider.embed_documents(["first", "second"])
        await provider.embed_query("hello")

    assert json.loads(route.calls[0].request.content)["input"] == ["first", "second"]
    assert json.loads(route.calls[1].request.content)["input"] == ["hello"]


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_endpoint_rejects_wrong_dimension() -> None:
    respx.post(OPENAI_ENDPOINT).mock(return_value=httpx.Response(200, json=_openai_body([[0.0] * 768])))
    settings = Settings(embedding_inference_url=OPENAI_ENDPOINT, embedding_dimension=1024)

    async with httpx.AsyncClient() as client:
        with pytest.raises(EmbeddingUnavailableError):
            await HuggingFaceEmbeddingProvider(settings, client).embed_query("hello")