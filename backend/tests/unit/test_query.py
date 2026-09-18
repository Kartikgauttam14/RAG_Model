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
