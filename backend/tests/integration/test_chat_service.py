import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.auth.dependencies import Principal
from app.chat.schemas import ChatRequest
from app.chat.service import ChatService
from app.config import Settings
from app.database.models import Conversation, Message, Role
from app.llm import LLMMessage, LLMResult, LLMUnavailableError
from app.rag.prompts import PromptRepository
from app.retrieval import RetrievalResult, RetrievedEvidence


class ScriptedLLM:
    def __init__(self, *, chunk_id: uuid.UUID) -> None:
        self.chunk_id = str(chunk_id)
        self.calls: list[list[LLMMessage]] = []

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        self.calls.append(messages)
        request = messages[-1].content
        response: dict[str, Any]
        if '"message"' in request:
            response = {"intent": "knowledge_question", "language": "en", "normalized_query": "delivery time"}
        elif '"draft_answer"' in request:
            response = {"supported": True, "answered_question": True, "recommended_action": "accept", "confidence": 0.9}
        elif '"question"' in request:
            response = {
                "answer": "Orders ship in two business days.",
                "citation_chunk_ids": [self.chunk_id],
                "grounded": True,
                "conflicts": [],
            }
        else:
            response = {"memories": []}
        return LLMResult(text=json.dumps(response), model="test")


class FailingLLM:
    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        raise LLMUnavailableError("provider unavailable")


class FakeRetriever:
    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.queries: list[str] = []

    async def retrieve(self, db: Any, **kwargs: Any) -> RetrievalResult:
        self.queries.append(kwargs["query"])
        return self.result

    async def count_evidence(self, db: Any, **_: Any) -> list[Any]:
        """The service asks for recorded count fields; these tests supply none."""
        return []


class SequentialRetriever:
    """Returns a different result per call and records the filters it was given."""

    def __init__(self, results: list[RetrievalResult]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, db: Any, **kwargs: Any) -> RetrievalResult:
        self.calls.append(kwargs)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]

    async def count_evidence(self, db: Any, **_: Any) -> list[Any]:
        return []


class FilteredPlannerLLM:
    """Plans with a `language` filter and only grounds the answer once retrieval widens."""

    def __init__(self, *, chunk_id: uuid.UUID, ground_on_widened_only: bool = True) -> None:
        self.chunk_id = str(chunk_id)
        self.ground_on_widened_only = ground_on_widened_only
        self.answers = 0

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        request = messages[-1].content
        if '"message"' in request:
            response: dict[str, Any] = {
                "intent": "knowledge_question",
                "language": "ar",
                "normalized_query": "كم عدد بوتيكات منصم في السعودية",
                "filters": {"language": "ar"},
            }
        elif '"draft_answer"' in request:
            response = {
                "supported": True,
                "answered_question": True,
                "recommended_action": "accept",
                "confidence": 0.9,
            }
        elif '"question"' in request:
            self.answers += 1
            ground = self.answers > 1 or not self.ground_on_widened_only
            response = {
                "answer": "ستة بوتيكات في السعودية.",
                "citation_chunk_ids": [self.chunk_id],
                "grounded": ground,
                "conflicts": [],
            }
        else:
            response = {"memories": []}
        return LLMResult(text=json.dumps(response), model="test")


class FakeMemory:
    def __init__(self) -> None:
        self.short_term: list[dict[str, str]] = []
        self.extracted = 0

    async def get_short_term(self, conversation_id: uuid.UUID) -> list[dict[str, str]]:
        return self.short_term

    async def append_short_term(self, conversation_id: uuid.UUID, role: str, content: str) -> None:
        self.short_term.append({"role": role, "content": content})

    async def extract_and_store(self, db: Any, **kwargs: Any) -> list[Any]:
        self.extracted += 1
        return []


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, Conversation | Message) and value.id is None:
                value.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1



def _evidence() -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Delivery Policy",
        document_version=2,
        content="Orders ship in two business days.",
        page_number=3,
        section="Delivery",
        metadata={},
        vector_score=0.92,
    )


def _service(llm: Any, retriever: Any, memory: Any) -> ChatService:
    # The doubles stand in for HybridRetriever/MemoryService, so they are typed as ``Any``
    # here instead of pretending to satisfy the real signatures. planner_llm_min_words=0
    # keeps the scripted LLM in charge of planning: these tests assert the plan the model
    # returns, and the default short-question fast path would skip that call.
    return ChatService(
        llm=llm,
        retriever=retriever,
        memory=memory,
        prompts=PromptRepository(Path(__file__).parents[3] / "prompts"),
        settings=Settings(planner_llm_min_words=0),
    )


@pytest.mark.asyncio
async def test_chat_service_persists_verified_answer_and_retrieval_candidates() -> None:
    evidence = _evidence()
    retrieval = RetrievalResult(
        evidence=[evidence],
        latency_ms=12,
        reranker_used=False,
        candidate_chunk_ids=[evidence.chunk_id],
        fallback_reason="reranker_not_configured_rrf_used",
    )
    llm = ScriptedLLM(chunk_id=evidence.chunk_id)
    memory = FakeMemory()
    retriever = FakeRetriever(retrieval)
    db: Any = FakeSession()
    events: list[str] = []

    async def record_event(state: str) -> None:
        events.append(state)

    response = await _service(llm, retriever, memory).respond(
        db,
        Principal(uuid.uuid4(), Role.admin, "default"),
        ChatRequest(message="How long is delivery?"),
        "request-1",
        record_event,
    )

    assert response.grounded
    assert response.verification_status == "verified"
    assert response.citations[0].chunk_id == evidence.chunk_id
    assert retriever.queries == ["delivery time"]
    assert memory.extracted == 1
    assert events == ["processing", "understanding", "retrieving", "reranking", "generating", "verifying", "complete"]
    retrieval_event = next(value for value in db.added if value.__class__.__name__ == "RetrievalEvent")
    assert retrieval_event.candidate_chunk_ids == [str(evidence.chunk_id)]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_chat_service_widens_retrieval_when_filtered_answer_is_ungrounded() -> None:
    filtered_evidence = _evidence()
    widened_evidence = _evidence()
    filtered = RetrievalResult(
        evidence=[filtered_evidence],
        latency_ms=5,
        reranker_used=False,
        candidate_chunk_ids=[filtered_evidence.chunk_id],
        fallback_reason="reranker_not_configured_rrf_used",
    )
    widened = RetrievalResult(
        evidence=[widened_evidence],
        latency_ms=7,
        reranker_used=False,
        candidate_chunk_ids=[widened_evidence.chunk_id, filtered_evidence.chunk_id],
        fallback_reason="reranker_not_configured_rrf_used",
    )
    llm = FilteredPlannerLLM(chunk_id=widened_evidence.chunk_id)
    retriever = SequentialRetriever([filtered, widened])
    memory = FakeMemory()
    db: Any = FakeSession()

    response = await _service(llm, retriever, memory).respond(
        db,
        Principal(uuid.uuid4(), Role.admin, "default"),
        ChatRequest(message="كم عدد بوتيكات منصم في السعودية؟"),
        "request-3",
    )

    # The planner's `language` filter hid the answer-bearing chunk, so the
    # service must retry with no filters and keep the grounded answer.
    assert [call["filters"] for call in retriever.calls] == [{"language": "ar"}, {}]
    assert response.grounded
    assert response.citations[0].chunk_id == widened_evidence.chunk_id
    retrieval_event = next(value for value in db.added if value.__class__.__name__ == "RetrievalEvent")
    assert retrieval_event.filters == {}
    assert retrieval_event.selected_chunk_ids == [str(widened_evidence.chunk_id)]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_chat_service_rolls_back_when_llm_is_unavailable() -> None:
    evidence = _evidence()
    retriever = FakeRetriever(
        RetrievalResult(evidence=[evidence], latency_ms=1, reranker_used=False, candidate_chunk_ids=[evidence.chunk_id])
    )
    db: Any = FakeSession()
    service = _service(FailingLLM(), retriever, FakeMemory())

    with pytest.raises(LLMUnavailableError):
        await service.respond(
            db,
            Principal(uuid.uuid4(), Role.admin, "default"),
            ChatRequest(message="Delivery?"),
            "request-2",
        )

    assert db.commits == 0
    assert db.rollbacks == 1
