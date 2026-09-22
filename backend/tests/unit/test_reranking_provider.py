import json
from typing import Any

import httpx
import pytest
import respx
from app.config import Settings
from app.reranking.base import RerankItem
from app.reranking.huggingface import HuggingFaceReranker, RerankerUnavailableError

TEI_ENDPOINT = "http://127.0.0.1:8082/rerank"
HF_ENDPOINT = "https://router.huggingface.co/hf-inference/models/BAAI/bge-reranker-v2-m3"


def _candidates(count: int) -> list[RerankItem]:
    return [RerankItem(f"c{index}", f"chunk {index}") for index in range(count)]


def _tei_body(scores_by_index: dict[int, float]) -> list[dict[str, object]]:
    """Text Embeddings Inference replies sorted by score, so ``index`` carries the rank."""
    return [
        {"index": index, "score": score}
        for index, score in sorted(scores_by_index.items(), key=lambda pair: pair[1], reverse=True)
    ]


@pytest.mark.asyncio
@respx.mock
async def test_tei_candidate_pool_larger_than_server_batch_is_split() -> None:
    """A pool above ``--max-client-batch-size`` must be split instead of failing with 413.

    Text Embeddings Inference defaults to a 32-pair client batch limit, so a 35-candidate pool
    has to become two requests. Scores are unique per candidate so any index misalignment
    across the batches would surface as a mismatched id.
    """
    request_batches: list[int] = []
    next_index = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal next_index
        texts = json_payload(request)["texts"]
        request_batches.append(len(texts))
        scores = {offset: float(next_index + offset) for offset in range(len(texts))}
        next_index += len(texts)
        return httpx.Response(200, json=_tei_body(scores))

    route = respx.post(TEI_ENDPOINT).mock(side_effect=respond)
    settings = Settings(rerank_inference_url=TEI_ENDPOINT)

    async with httpx.AsyncClient() as client:
        ranked = await HuggingFaceReranker(settings, client).rerank("query", _candidates(35), top_k=35)

    assert request_batches == [32, 3]
    assert route.call_count == 2
    assert [item.candidate_id for item in ranked] == [f"c{index}" for index in reversed(range(35))]
    assert [item.score for item in ranked] == [float(index) for index in reversed(range(35))]


@pytest.mark.asyncio
@respx.mock
async def test_tei_scores_are_realigned_to_candidate_order() -> None:
    """Batched replies are concatenated in candidate order before sorting and truncating."""
    bodies = [
        _tei_body({0: 0.1, 1: 0.9}),
        _tei_body({0: 0.3, 1: 0.4}),
        _tei_body({0: 0.5}),
    ]
    route = respx.post(TEI_ENDPOINT).mock(side_effect=[httpx.Response(200, json=body) for body in bodies])
    settings = Settings(rerank_inference_url=TEI_ENDPOINT, rerank_batch_size=2)

    async with httpx.AsyncClient() as client:
        ranked = await HuggingFaceReranker(settings, client).rerank("query", _candidates(5), top_k=3)

    assert [len(json_payload(call.request)["texts"]) for call in route.calls] == [2, 2, 1]
    assert [(item.candidate_id, item.score) for item in ranked] == [
        ("c1", 0.9),
        ("c4", 0.5),
        ("c3", 0.4),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_native_huggingface_endpoint_sends_text_pair_inputs() -> None:
    """The hosted HF pipeline needs ``inputs`` dict pairs and reports ``LABEL_1`` relevance."""
    route = respx.post(HF_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=[
                [
                    {"label": "LABEL_0", "score": 0.2},
                    {"label": "LABEL_1", "score": 0.8},
                ]
            ],
        )
    )
    settings = Settings(rerank_inference_url=HF_ENDPOINT, rerank_batch_size=2)

    async with httpx.AsyncClient() as client:
        ranked = await HuggingFaceReranker(settings, client).rerank("query", _candidates(2), top_k=2)

    assert json_payload(route.calls[0].request) == {
        "inputs": [
            {"text": "query", "text_pair": "chunk 0"},
            {"text": "query", "text_pair": "chunk 1"},
        ]
    }
    assert [item.score for item in ranked] == [0.8, 0.2]


@pytest.mark.asyncio
@respx.mock
async def test_http_error_is_reported_as_reranker_unavailable() -> None:
    respx.post(TEI_ENDPOINT).mock(return_value=httpx.Response(413))
    settings = Settings(rerank_inference_url=TEI_ENDPOINT)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RerankerUnavailableError):
            await HuggingFaceReranker(settings, client).rerank("query", _candidates(2), top_k=2)


def json_payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)
