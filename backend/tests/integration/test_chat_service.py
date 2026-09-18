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


def _service(llm: Any, retriever: FakeRetriever, memory: FakeMemory) -> ChatService:
    return ChatService(
        llm=llm,
        retriever=retriever,
        memory=memory,
        prompts=PromptRepository(Path(__file__).parents[3] / "prompts"),
        settings=Settings(),
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
    db = FakeSession()
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
async def test_chat_service_rolls_back_when_llm_is_unavailable() -> None:
    evidence = _evidence()
    retriever = FakeRetriever(
        RetrievalResult(evidence=[evidence], latency_ms=1, reranker_used=False, candidate_chunk_ids=[evidence.chunk_id])
    )
    db = FakeSession()
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
