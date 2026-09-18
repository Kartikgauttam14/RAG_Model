import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Document, DocumentChunk, DocumentStatus, DocumentVersion
from app.embeddings import EmbeddingProvider
from app.reranking import Reranker, RerankerUnavailableError, RerankItem


@dataclass(frozen=True)
class RetrievedEvidence:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    document_version: int
    content: str
    page_number: int | None
    section: str | None
    metadata: dict[str, Any]
    vector_score: float | None = None
    lexical_score: float | None = None
    fused_score: float = 0
    reranker_score: float | None = None


@dataclass(frozen=True)
class RetrievalResult:
    evidence: list[RetrievedEvidence]
    latency_ms: int
    reranker_used: bool
    candidate_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    fallback_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class HybridRetriever:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        reranker: Reranker | None,
        vector_top_k: int,
        lexical_top_k: int,
        rerank_top_k: int,
    ) -> None:
        self.embedding_provider = embedding_provider
        self.reranker = reranker
        self.vector_top_k = vector_top_k
        self.lexical_top_k = lexical_top_k
        self.rerank_top_k = rerank_top_k

    async def retrieve(
        self,
        db: AsyncSession,
        *,
        query: str,
        tenant_id: str,
        user_id: uuid.UUID,
        role: str,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        query_embedding = await self.embedding_provider.embed_query(query)
        base_filters = self._filters(tenant_id, user_id, role, filters or {})

        vector_score = (1 - DocumentChunk.embedding.cosine_distance(query_embedding)).label("vector_score")
        vector_stmt = (
            self._base_select(vector_score).where(*base_filters).order_by(vector_score.desc()).limit(self.vector_top_k)
        )
        ts_query = func.websearch_to_tsquery("simple", query)
        lexical_score = func.ts_rank_cd(DocumentChunk.search_vector, ts_query).label("lexical_score")
        lexical_stmt = (
            self._base_select(lexical_score)
            .where(*base_filters, DocumentChunk.search_vector.op("@@")(ts_query))
            .order_by(lexical_score.desc())
            .limit(self.lexical_top_k)
        )
        vector_rows = list((await db.execute(vector_stmt)).all())
        lexical_rows = list((await db.execute(lexical_stmt)).all())
        merged = self._rrf_merge(vector_rows, lexical_rows)
        candidate_chunk_ids = [item.chunk_id for item in merged]

        reranker_used = False
        fallback_reason: str | None = None
        if self.reranker and merged:
            try:
                ranking = await self.reranker.rerank(
                    query,
                    [RerankItem(str(item.chunk_id), item.content) for item in merged],
                    self.rerank_top_k,
                )
                by_id = {str(item.chunk_id): item for item in merged}
                merged = [
                    _replace_score(by_id[item.candidate_id], item.score)
                    for item in ranking
                    if item.candidate_id in by_id
                ]
                reranker_used = True
            except RerankerUnavailableError:
                fallback_reason = "reranker_unavailable_rrf_used"
                merged = merged[: self.rerank_top_k]
        else:
            merged = merged[: self.rerank_top_k]
            if self.reranker is None:
                fallback_reason = "reranker_not_configured_rrf_used"

        return RetrievalResult(
            evidence=merged,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reranker_used=reranker_used,
            candidate_chunk_ids=candidate_chunk_ids,
            fallback_reason=fallback_reason,
            diagnostics={
                "vector_candidates": len(vector_rows),
                "lexical_candidates": len(lexical_rows),
                "merged_candidates": len(merged),
                "candidate_chunk_ids": [str(item) for item in candidate_chunk_ids],
            },
        )

    @staticmethod
    def _base_select(score: Any) -> Select[Any]:
        return (
            select(
                DocumentChunk,
                Document.name,
                DocumentVersion.version,
                score,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .where(
                Document.status == DocumentStatus.ready,
                Document.deleted_at.is_(None),
                DocumentVersion.version == Document.current_version,
            )
        )

    @staticmethod
    def _filters(
        tenant_id: str,
        user_id: uuid.UUID,
        role: str,
        filters: dict[str, Any],
    ) -> list[Any]:
        conditions: list[Any] = [
            DocumentChunk.tenant_id == tenant_id,
            or_(
                DocumentChunk.access_scope == "public",
                DocumentChunk.access_scope == "tenant",
                DocumentChunk.access_scope == f"user:{user_id}",
                DocumentChunk.access_scope == f"role:{role}",
            ),
        ]
        if filters.get("language"):
            conditions.append(DocumentChunk.language == str(filters["language"])[:16])
        if filters.get("category"):
            conditions.append(DocumentChunk.category == str(filters["category"])[:100])
        if filters.get("document"):
            conditions.append(Document.name == str(filters["document"])[:500])
        if filters.get("version") is not None:
            conditions.append(DocumentVersion.version == int(filters["version"]))
        return conditions

    @staticmethod
    def _rrf_merge(vector_rows: list[Any], lexical_rows: list[Any], k: int = 60) -> list[RetrievedEvidence]:
        merged: dict[uuid.UUID, RetrievedEvidence] = {}
        scores: dict[uuid.UUID, float] = {}

        def add(rows: list[Any], source: str) -> None:
            for rank, row in enumerate(rows, 1):
                chunk: DocumentChunk = row[0]
                score = float(row[3])
                existing = merged.get(chunk.id)
                if existing is None:
                    merged[chunk.id] = RetrievedEvidence(
                        chunk_id=chunk.id,
                        document_id=chunk.document_id,
                        document_name=row[1],
                        document_version=row[2],
                        content=chunk.content,
                        page_number=chunk.page_number,
                        section=chunk.section,
                        metadata=chunk.metadata_json,
                        vector_score=score if source == "vector" else None,
                        lexical_score=score if source == "lexical" else None,
                    )
                else:
                    merged[chunk.id] = RetrievedEvidence(
                        **{
                            **existing.__dict__,
                            f"{source}_score": score,
                        }
                    )
                scores[chunk.id] = scores.get(chunk.id, 0) + 1 / (k + rank)

        add(vector_rows, "vector")
        add(lexical_rows, "lexical")
        ranked = [
            RetrievedEvidence(**{**item.__dict__, "fused_score": scores[item.chunk_id]}) for item in merged.values()
        ]
        return sorted(ranked, key=lambda item: item.fused_score, reverse=True)


def _replace_score(item: RetrievedEvidence, score: float) -> RetrievedEvidence:
    return RetrievedEvidence(**{**item.__dict__, "reranker_score": score})
