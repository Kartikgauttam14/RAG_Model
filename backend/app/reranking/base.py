from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RerankItem:
    candidate_id: str
    text: str
    score: float = 0.0


class Reranker(Protocol):
    async def rerank(self, query: str, candidates: list[RerankItem], top_k: int) -> list[RerankItem]: ...
