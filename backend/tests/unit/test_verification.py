import uuid
from pathlib import Path

import pytest
from app.llm import LLMMessage, LLMResult
from app.rag.prompts import PromptRepository
from app.retrieval import RetrievedEvidence
from app.verification import GroundedAnswerService


class ScriptedLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = iter(replies)

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        return LLMResult(text=next(self.replies), model="test")


def _evidence() -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Policy",
        document_version=1,
        content="Orders ship in two business days.",
        page_number=2,
        section="Delivery",
        metadata={},
        vector_score=0.9,
    )


def _prompts() -> PromptRepository:
    return PromptRepository(Path(__file__).parents[3] / "prompts")


@pytest.mark.asyncio
async def test_invalid_citation_falls_back_to_top_evidence() -> None:
    evidence = _evidence()
    llm = ScriptedLLM(
        [
            '{"answer":"Ships tomorrow","citation_chunk_ids":["invented"],"grounded":true}',
            '{"supported":true,"answered_question":true,"recommended_action":"accept","confidence":1}',
        ]
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000)

    answer = await service.answer(question="When?", language="en", evidence=[evidence])

    assert answer.grounded
    assert answer.verification_status == "verified"
    assert answer.citations[0].chunk_id == str(evidence.chunk_id)


@pytest.mark.asyncio
async def test_index_based_citations_are_resolved() -> None:
    evidence = _evidence()
    llm = ScriptedLLM(
        [
            '{"answer":"Orders ship in two business days.","citation_ids":[1],"grounded":true}',
            '{"supported":true,"answered_question":true,"recommended_action":"accept","confidence":0.9}',
        ]
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000)

    answer = await service.answer(question="When?", language="en", evidence=[evidence])

    assert answer.grounded
    assert answer.citations[0].chunk_id == str(evidence.chunk_id)
    assert "two business days" in answer.answer


@pytest.mark.asyncio
async def test_failed_verification_returns_best_effort_answer() -> None:
    llm = ScriptedLLM(
        [
            '{"answer":"Ships tomorrow","citation_ids":[1],"grounded":true}',
            '{"supported":false,"answered_question":true,"recommended_action":"regenerate","confidence":0.2}',
            '{"answer":"Orders ship in two business days.","citation_ids":[1],"grounded":true}',
            '{"supported":false,"answered_question":true,"recommended_action":"refuse","confidence":0.1}',
        ]
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000)

    answer = await service.answer(question="When?", language="en", evidence=[_evidence()])

    assert not answer.grounded
    assert answer.verification_status == "unverified_fallback"
    assert "two business days" in answer.answer
    assert answer.citations


@pytest.mark.asyncio
async def test_verification_refuses_without_sufficient_retrieval() -> None:
    service = GroundedAnswerService(ScriptedLLM([]), _prompts(), 0.95, 1, 4000)

    answer = await service.answer(question="When?", language="en", evidence=[_evidence()])

    assert not answer.grounded
    assert answer.verification_status == "insufficient_evidence"