from pathlib import Path

import pytest
from app.chat.query import QueryPlanner
from app.llm import LLMMessage, LLMResult
from app.rag.prompts import PromptRepository


class ScriptedLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        return LLMResult(text=self.reply, model="test")


def _prompts() -> PromptRepository:
    return PromptRepository(Path(__file__).parents[3] / "prompts")


@pytest.mark.asyncio
async def test_fallback_marks_history_reference_ambiguous_without_history() -> None:
    planner = QueryPlanner(None, _prompts())

    plan = await planner.plan("What about the second one?", [])

    assert plan.depends_on_history
    assert plan.ambiguous
    assert plan.clarification_question
    assert plan.normalized_query == "What about the second one?"


@pytest.mark.asyncio
async def test_fallback_detects_arabic_and_preserves_language_filter() -> None:
    planner = QueryPlanner(None, _prompts())

    plan = await planner.plan("ما مدة التوصيل؟", [])

    assert plan.language == "ar"
    assert plan.filters == {"language": "ar"}


@pytest.mark.asyncio
async def test_malformed_llm_plan_falls_back_to_safe_plan() -> None:
    planner = QueryPlanner(ScriptedLLM("not json"), _prompts())

    plan = await planner.plan("How long is delivery?", [])

    assert plan.intent == "knowledge_question"
    assert plan.retrieval_required
    assert plan.normalized_query == "How long is delivery?"


class RecordingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages: list[LLMMessage], **_: object) -> LLMResult:
        self.calls += 1
        return LLMResult(text='{"normalized_query": "delivery time", "language": "en"}', model="test")


@pytest.mark.asyncio
async def test_short_first_turn_question_skips_the_planner_generation() -> None:
    llm = RecordingLLM()
    planner = QueryPlanner(llm, _prompts(), min_words_for_llm=6)

    plan = await planner.plan("كم مدة التوصيل؟", [])

    assert llm.calls == 0
    assert plan.language == "ar"
    assert plan.normalized_query == "كم مدة التوصيل؟"


@pytest.mark.asyncio
async def test_force_llm_escalates_a_short_message_to_the_planner() -> None:
    """A greeting has no entities to retrieve on, so the plan must be escalated.

    The cheap deterministic plan sends "hi" to the retriever verbatim and the answer came
    back ungrounded; the model's rewrite is what reaches the greeting sheet.
    """
    llm = RecordingLLM()
    planner = QueryPlanner(llm, _prompts(), min_words_for_llm=6)

    cheap = await planner.plan("hi", [])
    assert llm.calls == 0
    assert cheap.planned_by == "deterministic"
    assert cheap.normalized_query == "hi"

    escalated = await planner.plan("hi", [], force_llm=True)
    assert llm.calls == 1
    assert escalated.planned_by == "llm"
    assert escalated.normalized_query == "delivery time"


@pytest.mark.asyncio
async def test_long_or_follow_up_question_still_uses_the_planner() -> None:
    llm = RecordingLLM()
    planner = QueryPlanner(llm, _prompts(), min_words_for_llm=6)

    plan = await planner.plan("How long is delivery for an order today?", [])

    assert llm.calls == 1
    assert plan.normalized_query == "delivery time"

    # A follow-up needs history resolved, so the model is asked even for a short message.
    llm.calls = 0
    await planner.plan("and that one?", [{"role": "user", "content": "Tell me about Qanun"}])
    assert llm.calls == 1
