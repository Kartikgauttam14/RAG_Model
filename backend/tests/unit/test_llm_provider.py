import httpx
import pytest
import respx
from app.config import Settings
from app.llm import LLMMessage
from app.llm.huggingface import HuggingFaceLLMProvider


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
