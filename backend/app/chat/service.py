import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.chat.query import QueryPlanner
from app.chat.schemas import ChatRequest, ChatResponse, CitationResponse
from app.config import Settings
from app.database.models import (
    AnswerVerification,
    Conversation,
    Message,
    MessageRole,
    RetrievalEvent,
)
from app.llm import LLMProvider, LLMUnavailableError
from app.memory import MemoryService
from app.monitoring.logging import get_logger
from app.rag.prompts import PromptRepository
from app.retrieval import HybridRetriever
from app.verification import GroundedAnswerService

ProgressCallback = Callable[[str], Awaitable[None]]
logger = get_logger(__name__)


class ChatService:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        retriever: HybridRetriever,
        memory: MemoryService,
        prompts: PromptRepository,
        settings: Settings,
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.memory = memory
        self.planner = QueryPlanner(llm, prompts)
        self.answerer = GroundedAnswerService(
            llm,
            prompts,
            settings.rag_min_score,
            settings.rag_min_evidence,
            settings.rag_max_context_chars,
        )

    async def respond(
        self,
        db: AsyncSession,
        principal: Principal,
        request: ChatRequest,
        request_id: str,
        progress: ProgressCallback | None = None,
    ) -> ChatResponse:
        started = time.perf_counter()

        async def emit(state: str) -> None:
            if progress:
                await progress(state)

        await emit("processing")
        conversation = await self._conversation(db, principal, request)
        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.user,
            content=request.message,
            input_type="text",
            request_id=request_id,
        )
        db.add(user_message)
        await db.flush()
        history = await self.memory.get_short_term(conversation.id)
        await self.memory.append_short_term(conversation.id, "user", request.message)
        await emit("understanding")
        try:
            plan = await self.planner.plan(request.message, history)
        except LLMUnavailableError:
            logger.warning("llm_unavailable_during_planning", request_id=request_id)
            await db.rollback()
            raise
        if request.language:
            plan = type(plan)(**{**plan.__dict__, "language": request.language})
        if plan.ambiguous and plan.clarification_question:
            response = await self._persist_answer(
                db,
                conversation,
                request_id,
                plan.clarification_question,
                0,
                [],
                False,
                "clarification_required",
            )
            await db.commit()
            await emit("complete")
            return response

        await emit("retrieving")
        retrieval = await self.retriever.retrieve(
            db,
            query=plan.normalized_query,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role.value,
            filters=plan.filters,
        )
        if not retrieval.evidence and plan.filters:
            # Planner filters (e.g. language) can zero out retrieval; retry
            # unfiltered so the question can still be answered from the corpus.
            logger.info("retrieval_retry_without_filters", request_id=request_id)
            retrieval = await self.retriever.retrieve(
                db,
                query=plan.normalized_query,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                role=principal.role.value,
                filters={},
            )
        await emit("reranking")
        retrieval_event = RetrievalEvent(
            request_id=request_id,
            user_id=principal.user_id,
            conversation_id=conversation.id,
            query_hash=hashlib.sha256(plan.normalized_query.encode()).hexdigest(),
            filters=plan.filters,
            candidate_chunk_ids=[str(item) for item in retrieval.candidate_chunk_ids],
            selected_chunk_ids=[str(item.chunk_id) for item in retrieval.evidence],
            scores={
                str(item.chunk_id): {
                    "vector": item.vector_score,
                    "lexical": item.lexical_score,
                    "fused": item.fused_score,
                    "reranker": item.reranker_score,
                }
                for item in retrieval.evidence
            },
            latency_ms=retrieval.latency_ms,
        )
        db.add(retrieval_event)
        await emit("generating")
        try:
            answer = await self.answerer.answer(
                question=request.message,
                language=plan.language,
                evidence=retrieval.evidence,
            )
        except LLMUnavailableError:
            logger.warning("llm_unavailable", request_id=request_id)
            await db.rollback()
            raise
        await emit("verifying")
        response = await self._persist_answer(
            db,
            conversation,
            request_id,
            answer.answer,
            answer.confidence,
            [citation.__dict__ for citation in answer.citations],
            answer.grounded,
            answer.verification_status,
            conflicts=answer.conflicts,
            retrieval_fallback=retrieval.fallback_reason,
        )
        db.add(
            AnswerVerification(
                request_id=request_id,
                message_id=response.message_id,
                grounded=answer.grounded,
                status=answer.verification_status,
                confidence=answer.confidence,
                contradiction_notes=answer.conflicts,
                raw_result={"retrieval_fallback": retrieval.fallback_reason},
            )
        )
        await self.memory.append_short_term(conversation.id, "assistant", answer.answer)
        await self.memory.extract_and_store(
            db,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            source_message_id=user_message.id,
            text=request.message,
        )
        await db.commit()
        logger.info(
            "chat_completed",
            request_id=request_id,
            user_id=str(principal.user_id),
            conversation_id=str(conversation.id),
            grounded=answer.grounded,
            verification_status=answer.verification_status,
            total_latency_ms=int((time.perf_counter() - started) * 1000),
            retrieved_chunks=len(retrieval.evidence),
        )
        await emit("complete")
        return response

    @staticmethod
    async def _conversation(db: AsyncSession, principal: Principal, request: ChatRequest) -> Conversation:
        if request.conversation_id:
            conversation = await db.scalar(
                select(Conversation).where(
                    Conversation.id == request.conversation_id,
                    Conversation.user_id == principal.user_id,
                    Conversation.deleted_at.is_(None),
                )
            )
            if not conversation:
                raise ValueError("Conversation does not exist")
            return conversation
        conversation = Conversation(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            title=request.message[:80],
            language=request.language,
        )
        db.add(conversation)
        await db.flush()
        return conversation

    @staticmethod
    async def _persist_answer(
        db: AsyncSession,
        conversation: Conversation,
        request_id: str,
        text: str,
        confidence: float,
        citations: list[dict[str, Any]],
        grounded: bool,
        verification_status: str,
        *,
        conflicts: list[str] | None = None,
        retrieval_fallback: str | None = None,
    ) -> ChatResponse:
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.assistant,
            content=text,
            confidence=confidence,
            citations=citations,
            verification_status=verification_status,
            request_id=request_id,
        )
        db.add(message)
        await db.flush()
        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation.id,
            message_id=message.id,
            answer=text,
            confidence=confidence,
            citations=[CitationResponse(**item) for item in citations],
            grounded=grounded,
            verification_status=verification_status,
            conflicts=conflicts or [],
            retrieval_fallback=retrieval_fallback,
        )
