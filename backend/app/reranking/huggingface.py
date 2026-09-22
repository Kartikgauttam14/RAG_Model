from typing import Any

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

    @property
    def is_tei(self) -> bool:
        """Text Embeddings Inference exposes ranking at ``/rerank`` with its own payload."""
        return self.endpoint.rstrip("/").endswith("/rerank")

    async def rerank(self, query: str, candidates: list[RerankItem], top_k: int) -> list[RerankItem]:
        if not candidates:
            return []
        raw_scores = await self._score_all(query, candidates)
        ranked = [
            RerankItem(item.candidate_id, item.text, float(raw_scores[index]))
            for index, item in enumerate(candidates)
        ]
        return sorted(ranked, key=lambda item: item.score, reverse=True)[:top_k]

    async def _score_all(self, query: str, candidates: list[RerankItem]) -> list[float]:
        """Score every candidate, splitting into server-sized batches.

        Text Embeddings Inference rejects client batches larger than
        ``--max-client-batch-size`` (32 by default) with HTTP 413, which would silently
        disable reranking for any query whose candidate pool exceeds that size. Scores are
        concatenated in candidate order so indices stay aligned with ``candidates``.
        """
        batch_size = self.settings.rerank_batch_size
        scores: list[float] = []
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            scores.extend(await self._score_batch(query, batch))
        return scores

    async def _score_batch(self, query: str, batch: list[RerankItem]) -> list[float]:
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"} if self.settings.hf_token else {}
        # The two routes send different shapes, so the type is annotated rather than
        # inferred from the first branch (TEI wants strings, native HF wants text pairs).
        payload: dict[str, Any] = (
            {"query": query, "texts": [item.text for item in batch]}
            if self.is_tei
            else {"inputs": [{"text": query, "text_pair": item.text} for item in batch]}
        )
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.settings.rerank_timeout_seconds,
            )
            response.raise_for_status()
            raw_scores = self._extract_scores(response.json(), len(batch))
            if raw_scores is None:
                raise RerankerUnavailableError("Reranker returned an invalid response")
            return raw_scores
        except (httpx.TimeoutException, httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, RerankerUnavailableError):
                raise
            raise RerankerUnavailableError("Reranker is unavailable") from exc

    @staticmethod
    def _extract_scores(body: object, expected: int) -> list[float] | None:
        """Normalize HF text-classification output into one relevance score per candidate.

        The pipeline wraps the batch in an extra list level and tags each pair with a
        label, so both ``[[{label, score}, ...]]`` and a bare ``[0.1, 0.2]`` are valid.
        """
        payload = body.get("scores", body) if isinstance(body, dict) else body
        if not isinstance(payload, list):
            return None
        while len(payload) == 1 and isinstance(payload[0], list):
            payload = payload[0]
        if not payload or len(payload) != expected:
            return None
        if all(isinstance(entry, int | float) for entry in payload):
            return [float(entry) for entry in payload]
        if all(isinstance(entry, dict) and "index" in entry and "score" in entry for entry in payload):
            # Text Embeddings Inference returns ``[{index, score}]`` sorted by score, so the
            # rank must be read from ``index`` rather than the position in the list.
            scores = [0.0] * expected
            for entry in payload:
                index = int(entry["index"])
                if not 0 <= index < expected:
                    return None
                scores[index] = float(entry["score"])
            return scores
        pair_scores: list[float] = []
        for entry in payload:
            labels = entry if isinstance(entry, list) else [entry]
            values: dict[str, float] = {}
            for label in labels:
                if not isinstance(label, dict) or "score" not in label:
                    return None
                values[str(label.get("label", ""))] = float(label["score"])
            if not values:
                return None
            pair_scores.append(values.get("LABEL_1", max(values.values())))
        return pair_scores
