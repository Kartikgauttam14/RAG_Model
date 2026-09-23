from typing import Any

import httpx
import pytest
import respx
from app.api.routes.health import _check_database, _check_llm, _check_redis, _check_vector_store
from app.config import Settings


class DownDatabase:
    async def execute(self, statement: object) -> None:
        raise ConnectionError("database unavailable")


class DownRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_readiness_probes_report_independent_failures() -> None:
    import asyncio

    # The doubles imitate a closed session and a dead Redis, so they are annotated as
    # ``Any`` rather than pretending to satisfy AsyncSession/Redis structurally.
    database: Any = DownDatabase()
    redis: Any = DownRedis()
    result = await asyncio.gather(_check_database(database), _check_redis(redis))

    assert result == [False, False]


@pytest.mark.asyncio
async def test_vector_store_check_is_skipped_on_pgvector_backend() -> None:
    settings = Settings(vector_store_backend="pgvector")

    assert await _check_vector_store(settings) is None


@pytest.mark.asyncio
@respx.mock
async def test_vector_store_check_reports_qdrant_health() -> None:
    settings = Settings(vector_store_backend="qdrant", qdrant_url="http://qdrant:6333")
    route = respx.get("http://qdrant:6333/collections/mansam_chunks").mock(return_value=httpx.Response(200, json={}))

    assert await _check_vector_store(settings) is True
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_vector_store_check_reports_qdrant_outage() -> None:
    settings = Settings(vector_store_backend="qdrant", qdrant_url="http://qdrant:6333")
    respx.get("http://qdrant:6333/collections/mansam_chunks").mock(return_value=httpx.Response(500, json={}))

    assert await _check_vector_store(settings) is False


@pytest.mark.asyncio
async def test_llm_check_is_skipped_without_url() -> None:
    settings = Settings(hf_inference_url=None)

    assert await _check_llm(settings) is None


@pytest.mark.asyncio
async def test_llm_check_flags_loopback_url_without_network() -> None:
    settings = Settings(hf_inference_url="http://127.0.0.1:11434")

    result = await _check_llm(settings)

    assert result is not None
    assert result["ok"] is False
    assert result["reason"] == "unreachable_endpoint"
    assert "HF_INFERENCE_URL" in result["hint"]


@pytest.mark.asyncio
@respx.mock
async def test_llm_check_reports_ok_when_probe_succeeds() -> None:
    settings = Settings(
        hf_token="t" * 10,
        hf_model="test-model",
        hf_inference_url="https://router.huggingface.co",
    )
    respx.post("https://router.huggingface.co/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"model": "test-model", "choices": [{"message": {"content": "ok"}}]},
        )
    )

    result = await _check_llm(settings)

    assert result == {"ok": True, "reason": "ok", "model": "test-model"}


@pytest.mark.asyncio
@respx.mock
async def test_llm_check_reports_provider_reason() -> None:
    settings = Settings(
        hf_token="bad-token-12",  # noqa: S106 - obviously fake test credential
        hf_model="test-model",
        hf_inference_url="https://router.huggingface.co",
    )
    respx.post("https://router.huggingface.co/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"}),
    )

    result = await _check_llm(settings)

    assert result is not None
    assert result["ok"] is False
    assert result["reason"] == "unauthorized"
