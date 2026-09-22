import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.chat.query import QueryPlan, QueryPlanner
from app.chat.schemas import ChatRequest, ChatResponse, CitationResponse
from app.config import Settings
from app.database.models import (
    AnswerVerification,
    Conversation,
    Message,
    MessageRole,
    RetrievalEvent,
)
from app.database.session import SessionFactory
from app.llm import LLMProvider, LLMUnavailableError
from app.memory import MemoryService
from app.monitoring.logging import get_logger
from app.rag.prompts import PromptRepository
from app.retrieval import HybridRetriever, RetrievalResult
from app.verification import GroundedAnswer, GroundedAnswerService

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
        self.prompts = prompts
        self.settings = settings
        self._background: set[asyncio.Task[None]] = set()
        self.planner = QueryPlanner(llm, prompts, settings.planner_llm_min_words, settings.llm_fast_model)
        self.answerer = GroundedAnswerService(
            llm,
            prompts,
            settings.rag_min_score,
            settings.rag_min_evidence,
            settings.rag_max_context_chars,
            draft_model=settings.llm_draft_model,
            fast_model=settings.llm_fast_model,
            verify_enabled=settings.rag_verify_enabled,
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
        used_filters = plan.filters
        retrieval = await self._retrieve(db, principal, plan, filters=used_filters)
        if not retrieval.evidence and used_filters:
            # Planner filters (e.g. language) can zero out retrieval; retry
            # unfiltered so the question can still be answered from the corpus.
            logger.info("retrieval_retry_without_filters", request_id=request_id)
            used_filters = {}
            retrieval = await self._retrieve(db, principal, plan, filters=used_filters)
        await emit("reranking")
        await emit("generating")
        answer = await self._answer(db, request.message, plan.language, retrieval, request_id)
        if not answer.grounded and plan.planned_by == "deterministic":
            # The cheap plan sends a short message to the retriever verbatim, which is
            # enough for "price of Mamlakati" but not for a greeting like "hi", where the
            # model's rewrite is what reaches the greeting sheet. Escalate to the planner
            # once before falling back to a refusal.
            logger.info("planner_escalated", request_id=request_id)
            await emit("understanding")
            escalated = await self.planner.plan(request.message, history, force_llm=True)
            if escalated.planned_by == "llm":
                await emit("retrieving")
                escalated_filters = escalated.filters
                escalated_retrieval = await self._retrieve(db, principal, escalated, filters=escalated_filters)
                if not escalated_retrieval.evidence and escalated_filters:
                    escalated_filters = {}
                    escalated_retrieval = await self._retrieve(
                        db, principal, escalated, filters=escalated_filters
                    )
                await emit("reranking")
                await emit("generating")
                escalated_answer = await self._answer(
                    db, request.message, escalated.language, escalated_retrieval, request_id
                )
                if escalated_answer.grounded or escalated_answer.confidence > answer.confidence:
                    plan = escalated
                    used_filters = escalated_filters
                    retrieval = escalated_retrieval
                    answer = escalated_answer
        if used_filters and not answer.grounded:
            # Retrieval can rank an on-topic-looking but fact-free chunk above
            # the chunk that actually holds the answer (e.g. an Arabic question
            # whose fact only exists in an English settings sheet). Planner
            # filters such as `language` hide that chunk, so widen once,
            # unfiltered, and keep whichever answer the verifier trusts more.
            logger.info("retrieval_widened_retry", request_id=request_id)
            await emit("retrieving")
            widened = await self._retrieve(db, principal, plan, filters={})
            await emit("reranking")
            await emit("generating")
            widened_answer = await self._answer(
                db, request.message, plan.language, widened, request_id
            )
            if widened_answer.grounded or widened_answer.confidence > answer.confidence:
                used_filters = {}
                retrieval = widened
                answer = widened_answer
        await emit("verifying")
        db.add(
            self._retrieval_event(
                request_id=request_id,
                principal=principal,
                conversation=conversation,
                plan=plan,
                retrieval=retrieval,
                filters=used_filters,
            )
        )
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
        if self.settings.long_term_memory_inline:
            await self.memory.extract_and_store(
                db,
                user_id=principal.user_id,
                tenant_id=principal.tenant_id,
                source_message_id=user_message.id,
                text=request.message,
            )
        await db.commit()
        if not self.settings.long_term_memory_inline:
            self._schedule_memory_extraction(user_message.id, principal, request.message)
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

    def _schedule_memory_extraction(self, message_id: uuid.UUID, principal: Principal, text: str) -> None:
        """Remember a message without holding the answer behind it.

        Extraction costs one generation plus one embedding call, and the caller only
        needs the answer. The task opens its own session because the request's session
        is closed once the response is sent, and a failure here must never cost the
        user an answer.
        """
        if not self.settings.long_term_memory_enabled:
            return
        task = asyncio.create_task(self._extract_memory(message_id, principal, text))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _extract_memory(self, message_id: uuid.UUID, principal: Principal, text: str) -> None:
        try:
            async with SessionFactory() as db:
                await self.memory.extract_and_store(
                    db,
                    user_id=principal.user_id,
                    tenant_id=principal.tenant_id,
                    source_message_id=message_id,
                    text=text,
                )
                await db.commit()
        except Exception as exc:
            logger.warning("memory_extraction_failed", message_id=str(message_id), error=str(exc))

    async def _retrieve(
        self,
        db: AsyncSession,
        principal: Principal,
        plan: QueryPlan,
        *,
        filters: dict[str, Any],
    ) -> RetrievalResult:
        result = await self.retriever.retrieve(
            db,
            query=plan.normalized_query,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role.value,
            filters=filters,
        )
        counts = await self.retriever.count_evidence(
            db,
            query=plan.normalized_query,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role.value,
        )
        if not counts:
            return result
        # A recorded count outranks retrieved rows, so it goes first: the model quotes the
        # number instead of tallying evidence, and the citation resolves to the row it used.
        known = {item.chunk_id for item in result.evidence}
        additions = [item for item in counts if item.chunk_id not in known]
        if not additions:
            return result
        return replace(result, evidence=[*additions, *result.evidence])

    async def _answer(
        self,
        db: AsyncSession,
        question: str,
        language: str,
        retrieval: RetrievalResult,
        request_id: str,
    ) -> GroundedAnswer:
        try:
            return await self.answerer.answer(
                question=question,
                language=language,
                evidence=retrieval.evidence,
            )
        except LLMUnavailableError:
            logger.warning("llm_unavailable", request_id=request_id)
            await db.rollback()
            raise

    @staticmethod
    def _retrieval_event(
        *,
        request_id: str,
        principal: Principal,
        conversation: Conversation,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        filters: dict[str, Any],
    ) -> RetrievalEvent:
        return RetrievalEvent(
            request_id=request_id,
            user_id=principal.user_id,
            conversation_id=conversation.id,
            query_hash=hashlib.sha256(plan.normalized_query.encode()).hexdigest(),
            filters=filters,
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
