import httpx

from app.config import Settings
from app.monitoring.logging import get_logger

logger = get_logger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    pass


class HuggingFaceEmbeddingProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.embedding_inference_url:
            raise ValueError("EMBEDDING_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.embedding_inference_url
        self.client = client or httpx.AsyncClient(timeout=settings.embedding_timeout_seconds)

    @property
    def dimension(self) -> int:
        return self.settings.embedding_dimension

    @property
    def is_openai_compatible(self) -> bool:
        normalized = self.endpoint.lower()
        return "/v1/embeddings" in normalized or "integrate.api.nvidia.com" in normalized

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefix = self.settings.embedding_passage_prefix
        return await self._embed([f"{prefix}{text}" for text in texts])

    async def embed_query(self, text: str) -> list[float]:
        rows = await self._embed([f"{self.settings.embedding_query_prefix}{text}"])
        return rows[0]

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"} if self.settings.hf_token else {}
        try:
            if self.is_openai_compatible:
                response = await self.client.post(
                    self.endpoint,
                    headers={**headers, "Content-Type": "application/json"},
                    json={"input": inputs, "model": self.settings.embedding_model},
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, list) or len(data) != len(inputs):
                    raise EmbeddingUnavailableError("Embedding endpoint returned an invalid OpenAI-compatible batch")
                rows = [item.get("embedding") for item in data if isinstance(item, dict)]
                if len(rows) != len(inputs):
                    raise EmbeddingUnavailableError("Embedding endpoint returned an incomplete OpenAI-compatible batch")
            else:
                response = await self.client.post(
                    self.endpoint,
                    headers=headers,
                    json={"inputs": inputs, "options": {"wait_for_model": True}},
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                rows = body.get("embeddings", body) if isinstance(body, dict) else body

            if not isinstance(rows, list) or len(rows) != len(inputs):
                raise EmbeddingUnavailableError("Embedding endpoint returned an invalid batch")
            normalized: list[list[float]] = []
            for row in rows:
                if row is None:
                    raise EmbeddingUnavailableError("Embedding endpoint returned a null row")
                vector = [float(value) for value in row]
                if len(vector) != self.dimension:
                    raise EmbeddingUnavailableError(f"Expected embedding dimension {self.dimension}, got {len(vector)}")
                normalized.append(vector)
            return normalized
        except (httpx.TimeoutException, httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, EmbeddingUnavailableError):
                raise
            # The wrapper message alone hides whether the endpoint returned a status, refused
            # the connection or timed out, which is exactly what an outage investigation needs.
            logger.warning(
                "embedding_request_failed",
                endpoint=self.endpoint,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise EmbeddingUnavailableError("Embedding provider is unavailable") from exc
