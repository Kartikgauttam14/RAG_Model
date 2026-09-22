import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Document, DocumentChunk, DocumentStatus, DocumentVersion
from app.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from app.monitoring.logging import get_logger
from app.rag.counts import is_count_question
from app.reranking import Reranker, RerankerUnavailableError, RerankItem

logger = get_logger(__name__)

DEFAULT_LEXICAL_CONFIG = "english"


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
        lexical_config: str = DEFAULT_LEXICAL_CONFIG,
    ) -> None:
        self.embedding_provider = embedding_provider
        self.reranker = reranker
        self.vector_top_k = vector_top_k
        self.lexical_top_k = lexical_top_k
        self.rerank_top_k = rerank_top_k
        self.lexical_config = lexical_config

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
        vector_rows: list[Any] = []
        fallback_reason: str | None = None
        try:
            query_embedding = await self.embedding_provider.embed_query(query)
        except EmbeddingUnavailableError:
            # The vector arm needs the embedding endpoint; the lexical arm does not. Degrading
            # to full-text search keeps questions answerable when that endpoint is down or
            # cannot load its model, at the cost of recall on paraphrased questions.
            logger.warning("embedding_unavailable_lexical_only", query_chars=len(query))
            query_embedding = None
            fallback_reason = "embedding_unavailable_lexical_only"
        base_filters = self._filters(tenant_id, user_id, role, filters or {})

        if query_embedding is not None:
            vector_score = (1 - DocumentChunk.embedding.cosine_distance(query_embedding)).label("vector_score")
            vector_stmt = (
                self._base_select(vector_score)
                .where(*base_filters)
                .order_by(vector_score.desc())
                .limit(self.vector_top_k)
            )
            vector_rows = list((await db.execute(vector_stmt)).all())
        ts_query = self._lexical_tsquery(query)
        lexical_score = func.ts_rank_cd(DocumentChunk.search_vector, ts_query).label("lexical_score")
        lexical_stmt = (
            self._base_select(lexical_score)
            .where(*base_filters, DocumentChunk.search_vector.op("@@")(ts_query))
            .order_by(lexical_score.desc())
            .limit(self.lexical_top_k)
        )
        lexical_rows = list((await db.execute(lexical_stmt)).all())
        merged = self._rrf_merge(vector_rows, lexical_rows)
        candidate_chunk_ids = [item.chunk_id for item in merged]

        reranker_used = False
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
                # A missing embedding provider is the more fundamental degradation, so it
                # keeps the reason when both stages fail.
                fallback_reason = fallback_reason or "reranker_unavailable_rrf_used"
                merged = merged[: self.rerank_top_k]
        else:
            merged = merged[: self.rerank_top_k]
            if self.reranker is None:
                fallback_reason = fallback_reason or "reranker_not_configured_rrf_used"

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

    async def count_evidence(
        self,
        db: AsyncSession,
        *,
        query: str,
        tenant_id: str,
        user_id: uuid.UUID,
        role: str,
    ) -> list[RetrievedEvidence]:
        """Fetch the recorded count fields when the question asks how many.

        A row such as ``Setting Key: boutique_count_ksa / Value: 6`` answers the question
        exactly, but it competes for the top-k with dozens of neighbouring rows and was measured
        to be missing from the retrieved set, which left the model tallying rows and answering
        4, 6 and "five" on different runs. Counting questions read the count rows directly,
        under the same visibility filters as retrieval.
        """
        if not is_count_question(query):
            return []
        stmt = (
            self._base_select(literal(1.0))
            .where(
                *self._filters(tenant_id, user_id, role, {}),
                DocumentChunk.content.op("~*")("Setting Key: [a-z_]*_count_[a-z_]*"),
            )
            .limit(2)
        )
        rows = list((await db.execute(stmt)).all())
        if not rows:
            logger.info("count_fields_not_found", query_chars=len(query))
            return []
        return self._rrf_merge([], rows)

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

    def _lexical_tsquery(self, query: str) -> Any:
        """Build an OR-based full text query so the lexical arm matches any term.

        ``websearch_to_tsquery`` interprets space separated words as an AND that
        includes stop words, so a natural language question only matches a chunk
        containing every word verbatim. Joining the extracted terms with the ``or``
        operator restores recall, and the text search configuration is shared with
        the indexed ``search_vector`` so both sides use the same lexemes.
        """
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            return func.websearch_to_tsquery(self.lexical_config, query)
        return func.websearch_to_tsquery(self.lexical_config, " or ".join(terms))

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
