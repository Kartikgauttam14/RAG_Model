import uuid
from pathlib import Path
from typing import cast

import pytest
from app.database.models import AnswerVerification, Message
from app.llm import LLMMessage, LLMResult
from app.rag.prompts import PromptRepository
from app.retrieval import RetrievedEvidence
from app.verification import GroundedAnswerService
from sqlalchemy import String


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


def test_verification_status_columns_fit_all_service_statuses() -> None:
    """The longest status the service writes must fit the column, or the insert fails.

    ``model_reported_insufficient_evidence`` (36 characters) used to exceed a
    ``varchar(32)`` column, which turned a refusal into a failed request.
    """
    longest_status = "model_reported_insufficient_evidence"

    message_status = cast(String, Message.__table__.c.verification_status.type)
    answer_status = cast(String, AnswerVerification.__table__.c.status.type)
    assert message_status.length is not None and message_status.length >= len(longest_status)
    assert answer_status.length is not None and answer_status.length >= len(longest_status)


class RecordingLLM:
    """Captures the payload it was sent so a test can assert on the prompt contract."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.payloads: list[str] = []

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        self.payloads.append(messages[-1].content)
        return LLMResult(text=self.reply, model="test")


def _count_evidence() -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Mansam_SSOT_Master_v3_6.xlsx",
        document_version=1,
        content=(
            "Sheet: 10_Settings\nRow: 38\nSetting Key: boutique_count_ksa\nValue: 6\n"
            "Description: Number of KSA boutiques (Riyadh x3, Jeddah, Makkah, Madinah)\n"
            "Last Updated: 2026-06-15"
        ),
        page_number=None,
        section="10_Settings",
        metadata={},
        vector_score=0.9,
    )


@pytest.mark.asyncio
async def test_counting_question_receives_the_recorded_count_field() -> None:
    """A counting question must be handed the recorded value, not left to tally rows."""
    evidence = _count_evidence()
    llm = RecordingLLM(
        '{"answer":"There are 6 boutiques.","citation_ids":[1],"grounded":true,"conflicts":[]}'
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000, verify_enabled=False)

    answer = await service.answer(
        question="How many Mansam boutiques are there in Saudi Arabia?",
        language="en",
        evidence=[evidence],
    )

    payload = llm.payloads[0]
    assert '"authoritative_counts"' in payload
    assert '"boutique_count_ksa"' in payload
    assert '"value": "6"' in payload
    assert '"evidence_index": 1' in payload
    assert "Never count evidence rows yourself" in payload
    assert answer.answer == "There are 6 boutiques."


@pytest.mark.asyncio
async def test_non_counting_question_does_not_receive_count_fields() -> None:
    evidence = _count_evidence()
    llm = RecordingLLM(
        '{"answer":"Known from the price list.","citation_ids":[1],"grounded":true,"conflicts":[]}'
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000, verify_enabled=False)

    await service.answer(question="What is the price of Mamlakati?", language="en", evidence=[evidence])

    assert '"authoritative_counts"' not in llm.payloads[0]


@pytest.mark.asyncio
async def test_verification_can_be_skipped_for_a_latency_budget() -> None:
    """With verification off only the draft is generated, and the status records that.

    The scripted LLM holds a single reply, so a second generation would raise StopIteration.
    """
    evidence = _evidence()
    llm = ScriptedLLM(
        ['{"answer":"Ships in two business days","citation_ids":[1],"grounded":true,"conflicts":[]}']
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000, verify_enabled=False)

    answer = await service.answer(question="When?", language="en", evidence=[evidence])

    assert answer.grounded
    assert answer.verification_status == "verification_skipped"
    assert answer.citations[0].chunk_id == str(evidence.chunk_id)
    assert 0 < answer.confidence <= 0.75


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


@pytest.mark.asyncio
async def test_low_reranker_score_does_not_gate_retrieval() -> None:
    """A reranker probability must not be compared against the cosine threshold.

    Cross-encoder rerankers score relevant pairs far below ``min_score`` (0.04 here
    against a 0.5 threshold). Rejecting on that value made every question answer
    ``insufficient_evidence`` as soon as a reranker was configured.
    """
    evidence = RetrievedEvidence(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Policy",
        document_version=1,
        content="Orders ship in two business days.",
        page_number=2,
        section="Delivery",
        metadata={},
        vector_score=0.9,
        lexical_score=0.3,
        fused_score=0.03,
        reranker_score=0.04,
    )
    llm = ScriptedLLM(
        [
            '{"answer":"Orders ship in two business days.","citation_ids":[1],"grounded":true}',
            '{"supported":true,"answered_question":true,"recommended_action":"accept","confidence":0.9}',
        ]
    )
    service = GroundedAnswerService(llm, _prompts(), 0.5, 1, 4000)

    answer = await service.answer(question="When?", language="en", evidence=[evidence])

    assert answer.grounded
    assert answer.verification_status == "verified"
    assert answer.citations[0].chunk_id == str(evidence.chunk_id)
