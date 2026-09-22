import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm import LLMMessage, LLMProvider
from app.rag.prompts import PromptRepository

ARABIC = re.compile(r"[\u0600-\u06ff]")
HISTORY_REFERENCE = re.compile(
    r"\b(it|that|those|one|ones|first|second|former|latter|previous)\b|"
    r"(الأول|الثاني|هذا|هذه|ذلك|تلك)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryPlan:
    intent: str
    language: str
    entities: dict[str, list[str]] = field(default_factory=dict)
    retrieval_required: bool = True
    depends_on_history: bool = False
    ambiguous: bool = False
    clarification_question: str | None = None
    normalized_query: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    # "deterministic" when the cheap rule-based plan was used for a short message, "llm"
    # when the model rewrote the query. The chat service escalates a deterministic plan to
    # the model once if its answer turns out to be ungrounded.
    planned_by: str = "deterministic"


class QueryPlanner:
    def __init__(
        self,
        llm: LLMProvider | None,
        prompts: PromptRepository,
        min_words_for_llm: int = 0,
        model: str | None = None,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.min_words_for_llm = min_words_for_llm
        self.model = model

    async def plan(self, query: str, history: list[dict[str, str]], *, force_llm: bool = False) -> QueryPlan:
        normalized = " ".join(query.split()).strip()
        if not normalized:
            raise ValueError("Query cannot be empty")
        fallback = self._fallback(normalized, history)
        if self.llm is None:
            return fallback
        if not force_llm and not history and len(normalized.split()) < self.min_words_for_llm:
            # A short first-turn question has no history to resolve and no entity to
            # disambiguate, so the deterministic plan is enough. Skipping the model
            # here removes one generation from the critical path of most voice and
            # one-line questions. A greeting is the exception the service handles by
            # escalating here with force_llm when the cheap plan finds no answer.
            return fallback
        context = history[-8:]
        try:
            result = await self.llm.generate(
                [
                    LLMMessage("system", self.prompts.load("query_rewrite/plan.md")),
                    LLMMessage(
                        "user",
                        json.dumps({"history": context, "message": normalized}, ensure_ascii=False),
                    ),
                ],
                temperature=0,
                max_tokens=500,
                response_format="json",
                model=self.model,
            )
            payload = _parse_json(result.text)
            return QueryPlan(
                intent=str(payload.get("intent", fallback.intent))[:80],
                language=str(payload.get("language", fallback.language))[:16],
                entities=_clean_entities(payload.get("entities", {})),
                retrieval_required=bool(payload.get("retrieval_required", True)),
                depends_on_history=bool(payload.get("depends_on_history", False)),
                ambiguous=bool(payload.get("ambiguous", False)),
                clarification_question=_optional_string(payload.get("clarification_question")),
                normalized_query=str(payload.get("normalized_query") or normalized)[:2000],
                filters=_clean_filters(payload.get("filters", {})),
                planned_by="llm",
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _fallback(query: str, history: list[dict[str, str]]) -> QueryPlan:
        language = "ar" if ARABIC.search(query) else "en"
        depends = bool(HISTORY_REFERENCE.search(query))
        ambiguous = depends and not history
        clarification = None
        if ambiguous:
            clarification = "ما الذي تشير إليه تحديداً؟" if language == "ar" else "What are you referring to?"
        return QueryPlan(
            intent="knowledge_question",
            language=language,
            retrieval_required=True,
            depends_on_history=depends,
            ambiguous=ambiguous,
            clarification_question=clarification,
            normalized_query=query,
            filters={"language": language},
        )


def _parse_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object returned")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Expected an object")
    return payload


def _clean_entities(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, items in value.items():
        if isinstance(items, list):
            result[str(key)[:50]] = [str(item)[:200] for item in items[:20]]
    return result


def _clean_filters(value: Any) -> dict[str, Any]:
    allowed = {"language", "category", "document", "version", "date", "tags"}
    return {str(k): v for k, v in value.items() if k in allowed} if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    return str(value)[:500] if value else None
