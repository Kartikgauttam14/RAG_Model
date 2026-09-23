import httpx
import pytest
import respx
from app.config import Settings
from app.llm import LLMMessage
from app.llm.huggingface import HuggingFaceLLMProvider, LLMUnavailableError, _classify_status, _retryable


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_llm_retries_transient_http_failure() -> None:
    route = respx.post("https://router.huggingface.co/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(503, json={"error": "temporarily unavailable"}),
            httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            ),
        ]
    )
    test_token = "t" * 10
    settings = Settings(
        hf_token=test_token,
        hf_model="test-model",
        hf_inference_url="https://router.huggingface.co",
    )

    async with httpx.AsyncClient() as client:
        result = await HuggingFaceLLMProvider(settings, client).generate([LLMMessage("user", "hello")])

    assert result.text == "OK"
    assert route.call_count == 2


def test_status_classification_names_actionable_causes() -> None:
    assert _classify_status(401, "Unauthorized")[0] == "unauthorized"
    assert _classify_status(402, "Payment Required")[0] == "quota_exhausted"
    assert _classify_status(404, "Model not found")[0] == "model_not_found"
    assert _classify_status(429, "Too many requests")[0] == "rate_limited"
    assert _classify_status(503, "Service unavailable")[0] == "provider_error"

    assert _retryable("provider_error") is True
    assert _retryable("timeout") is True
    assert _retryable("unauthorized") is False
    assert _retryable("model_not_found") is False
    assert _retryable("quota_exhausted") is False


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_llm_does_not_retry_unauthorized() -> None:
    route = respx.post("https://router.huggingface.co/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"}),
    )
    settings = Settings(
        hf_token="bad-token-12",  # noqa: S106 - obviously fake test credential
        hf_model="test-model",
        hf_inference_url="https://router.huggingface.co",
    )

    async with httpx.AsyncClient() as client:
        try:
            await HuggingFaceLLMProvider(settings, client).generate([LLMMessage("user", "hello")])
        except LLMUnavailableError as exc:
            assert exc.reason == "unauthorized"
            assert "HF_TOKEN" in str(exc)
            assert route.call_count == 1
            return
    raise AssertionError("expected LLMUnavailableError")


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_llm_names_missing_model() -> None:
    route = respx.post("https://router.huggingface.co/v1/chat/completions").mock(
        return_value=httpx.Response(404, json={"error": "Model not found"}),
    )
    settings = Settings(
        hf_token="t" * 10,
        hf_model="gemma3:4b",
        hf_inference_url="https://router.huggingface.co",
    )

    async with httpx.AsyncClient() as client:
        try:
            await HuggingFaceLLMProvider(settings, client).generate([LLMMessage("user", "hello")])
        except LLMUnavailableError as exc:
            assert exc.reason == "model_not_found"
            assert "HF_MODEL" in str(exc)
            assert route.call_count == 1
            return
    raise AssertionError("expected LLMUnavailableError")


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_llm_names_unreachable_endpoint_without_retry() -> None:
    route = respx.post("http://127.0.0.1:11434/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    settings = Settings(
        hf_token="t" * 10,
        hf_model="test-model",
        hf_inference_url="http://127.0.0.1:11434",
    )

    async with httpx.AsyncClient() as client:
        try:
            await HuggingFaceLLMProvider(settings, client).generate([LLMMessage("user", "hello")])
        except LLMUnavailableError as exc:
            assert exc.reason == "unreachable_endpoint"
            assert route.call_count == 1
            return
    raise AssertionError("expected LLMUnavailableError")
