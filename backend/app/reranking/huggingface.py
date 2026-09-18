import httpx

from app.config import Settings
from app.reranking.base import RerankItem


class RerankerUnavailableError(RuntimeError):
    pass


class HuggingFaceReranker:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.rerank_inference_url:
            raise ValueError("RERANK_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.rerank_inference_url
        self.client = client or httpx.AsyncClient(timeout=settings.rerank_timeout_seconds)

    async def rerank(self, query: str, candidates: list[RerankItem], top_k: int) -> list[RerankItem]:
        if not candidates:
            return []
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"} if self.settings.hf_token else {}
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                json={"query": query, "texts": [item.text for item in candidates]},
            )
            response.raise_for_status()
            body = response.json()
            raw_scores = body.get("scores", body) if isinstance(body, dict) else body
            if not isinstance(raw_scores, list) or len(raw_scores) != len(candidates):
                raise RerankerUnavailableError("Reranker returned an invalid response")
            ranked = [
                RerankItem(item.candidate_id, item.text, float(raw_scores[index]))
                for index, item in enumerate(candidates)
            ]
            return sorted(ranked, key=lambda item: item.score, reverse=True)[:top_k]
        except (httpx.TimeoutException, httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, RerankerUnavailableError):
                raise
            raise RerankerUnavailableError("Reranker is unavailable") from exc
