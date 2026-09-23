import asyncio
import time
from typing import Any, Literal

import httpx

from app.config import Settings
from app.llm.base import LLMMessage, LLMResult
from app.monitoring.logging import get_logger

logger = get_logger(__name__)

# Short machine-readable cause attached to every LLMUnavailableError so the
# API layer can tell the user *why* generation failed instead of one generic
# "language model is unavailable". Render deployments fail most often on
# unreachable_endpoint (localhost Ollama URL copied to the cloud) and
# model_not_found (Ollama tag instead of a provider model id).
LLMFailureReason = Literal[
    "unreachable_endpoint",
    "unauthorized",
    "quota_exhausted",
    "model_not_found",
    "rate_limited",
    "provider_error",
    "timeout",
    "empty_completion",
]


class LLMUnavailableError(RuntimeError):
    def __init__(self, message: str, *, reason: LLMFailureReason = "provider_error") -> None:
        super().__init__(message)
        self.reason = reason


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
            except LLMUnavailableError as exc:
                reason: LLMFailureReason = exc.reason
                if attempt == 2 or not _retryable(reason):
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
        # OpenRouter requires HTTP-Referer and X-Title headers for auth context
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"} if self.settings.hf_token else {}
        if self.endpoint.startswith("https://openrouter.ai"):
            headers["HTTP-Referer"] = self.settings.public_base_url or "https://mansam.ai"
            headers["X-Title"] = self.settings.app_name
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
                    raise LLMUnavailableError(
                        "Hugging Face endpoint returned no generated text", reason="empty_completion"
                    )
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
                raise LLMUnavailableError("Hugging Face endpoint returned no generated text", reason="empty_completion")
            return LLMResult(text=text, model=selected)
        except LLMUnavailableError:
            raise
        except httpx.TimeoutException as exc:
            logger.warning(
                "llm_request_failed",
                endpoint=self.endpoint,
                model=selected,
                reason="timeout",
                error=str(exc),
            )
            raise LLMUnavailableError(
                f"Language model timed out after {self.settings.llm_timeout_seconds}s "
                f"({self.endpoint}). The hosted model may be cold-loading; try again.",
                reason="timeout",
            ) from exc
        except httpx.HTTPStatusError as exc:
            reason, hint = _classify_status(exc.response.status_code, exc.response.text)
            logger.warning(
                "llm_request_failed",
                endpoint=self.endpoint,
                model=selected,
                reason=reason,
                status_code=exc.response.status_code,
                error=exc.response.text[:2000],
            )
            raise LLMUnavailableError(
                f"Language model request failed ({exc.response.status_code}): {hint}",
                reason=reason,
            ) from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "llm_request_failed",
                endpoint=self.endpoint,
                model=selected,
                reason="unreachable_endpoint",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise LLMUnavailableError(
                f"Language model endpoint is unreachable ({self.endpoint}). "
                "On Render this usually means a localhost Ollama URL was copied "
                "to the cloud — set HF_INFERENCE_URL to the hosted base URL.",
                reason="unreachable_endpoint",
            ) from exc


def _retryable(reason: LLMFailureReason) -> bool:
    """Only transient failures deserve a retry: 5xx, timeouts and rate limits.

    Auth/quota/model-id failures are deterministic — retrying the same request
    three times just triples the wait before the user sees the real cause.
    """
    return reason in {"provider_error", "timeout", "rate_limited"}


def _classify_status(status_code: int, body: str) -> tuple[LLMFailureReason, str]:
    """Map an HTTP failure to a machine reason plus an actionable hint."""
    lowered = (body or "").lower()
    if status_code == 401 or status_code == 403:
        return (
            "unauthorized",
            "the endpoint rejected the credentials — check HF_TOKEN (access, not expired).",
        )
    if status_code == 402 or "quota" in lowered or "credit" in lowered or "payment" in lowered:
        return (
            "quota_exhausted",
            "the provider reports no remaining credit — check billing/quota, then retry.",
        )
    if status_code == 404 or ("model" in lowered and "not found" in lowered):
        return (
            "model_not_found",
            "the model id is not served at this URL — check HF_MODEL "
            "(an Ollama tag like gemma3:4b won't work on Render; "
            "use e.g. meta-llama/Llama-3.1-8B-Instruct).",
        )
    if status_code == 429:
        return (
            "rate_limited",
            "the provider throttled the request — wait a minute and retry.",
        )
    if 500 <= status_code < 600:
        return (
            "provider_error",
            "the provider returned a server error — usually transient, retry shortly.",
        )
    return ("provider_error", f"unexpected status {status_code}: {body[:500]}")
