import asyncio
import time
from typing import Any

import httpx

from app.config import Settings
from app.llm.base import LLMMessage, LLMResult
from app.monitoring.logging import get_logger

logger = get_logger(__name__)


class LLMUnavailableError(RuntimeError):
    pass


class HuggingFaceLLMProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.hf_inference_url:
            raise ValueError("HF_INFERENCE_URL is required for the Hugging Face LLM provider")
        self.settings = settings
        self.endpoint = settings.hf_inference_url
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def generate(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        response_format: str | None = None,
        model: str | None = None,
    ) -> LLMResult:
        """Generate a completion, optionally on a specific model.

        ``model`` lets one deployment route each pipeline stage to a different served
        model: the answer draft can use the strongest model available while the planner,
        verifier and memory extractor use a smaller one, which is what keeps a
        multi-call pipeline fast when the large model does not fit on the GPU.
        """
        selected = model or self.settings.hf_model
        for attempt in range(3):
            try:
                return await self._generate_once(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    model=selected,
                )
            except LLMUnavailableError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise LLMUnavailableError("Hosted language model is unavailable")

    async def _generate_once(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        response_format: str | None = None,
        model: str | None = None,
    ) -> LLMResult:
        selected = model or self.settings.hf_model
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"} if self.settings.hf_token else {}
        try:
            if self.settings.hf_api_mode == "openai":
                url = self.endpoint.rstrip("/")
                if not url.endswith("/chat/completions"):
                    url += "/v1/chat/completions"
                payload: dict[str, Any] = {
                    "model": selected,
                    "messages": [m.__dict__ for m in messages],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format == "json":
                    payload["response_format"] = {"type": "json_object"}
                started = time.perf_counter()
                response = await self.client.post(
                    url, headers=headers, json=payload, timeout=self.settings.llm_timeout_seconds
                )
                response.raise_for_status()
                body = response.json()
                usage = body.get("usage", {})
                # Per-call timing is the only way to see where a slow answer spent its time:
                # one question is several sequential generations, and the model, the prompt
                # size and a cold model load all move the number independently.
                logger.info(
                    "llm_generate",
                    model=str(body.get("model", selected)),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    prompt_chars=sum(len(message.content) for message in messages),
                )
                text = body["choices"][0]["message"]["content"]
                if not isinstance(text, str) or not text.strip():
                    raise LLMUnavailableError("Hugging Face endpoint returned no generated text")
                return LLMResult(
                    text=text,
                    model=body.get("model", selected),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )

            prompt = "\n".join(f"{m.role.upper()}: {m.content}" for m in messages)
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                json={
                    "inputs": prompt,
                    "parameters": {
                        "temperature": max(temperature, 0.01),
                        "max_new_tokens": max_tokens,
                        "return_full_text": False,
                    },
                },
                timeout=self.settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            if isinstance(body, list):
                text = body[0].get("generated_text", "")
            else:
                text = body.get("generated_text", "")
            if not text:
                raise LLMUnavailableError("Hugging Face endpoint returned no generated text")
            return LLMResult(text=text, model=selected)
        except (httpx.TimeoutException, httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            error_details = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                error_details = f"{exc} | Response: {exc.response.text}"
            logger.error(
                "llm_request_failed",
                endpoint=self.endpoint,
                model=selected,
                error=error_details,
            )
            raise LLMUnavailableError(f"Hosted language model is unavailable: {error_details}") from exc

