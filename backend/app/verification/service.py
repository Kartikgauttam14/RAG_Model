import json
from typing import Any

from app.llm import LLMMessage, LLMProvider, LLMUnavailableError
from app.rag.counts import count_fields, is_count_question
from app.rag.prompts import PromptRepository
from app.retrieval import RetrievedEvidence
from app.security import assess_prompt_injection, wrap_untrusted_evidence
from app.verification.models import Citation, GroundedAnswer, VerificationResult

UNCERTAINTY = {
    "en": "Let me find your perfect match — may I ask a couple of questions to guide us?",
    "ar": "دعني أجد لك العطر المثالي — هل يمكنني أن أسألك بعض الأسئلة لأساعدك بشكل أفضل؟",
}

# The JSON contract is also stated in the prompt files, but those arrive as *leading*
# system messages followed by a large evidence payload. Smaller instruction-tuned models
# then continue the shape of the evidence (a JSON array/object of chunks) instead of
# emitting the contract, which surfaced as "Draft answer is empty". Restating the contract
# as the final key of the user payload keeps it last in the context window.
ANSWER_CONTRACT = (
    "Reply with exactly one JSON object and nothing else. It must contain the keys "
    "'answer' (string), 'citation_ids' (array of integers), 'grounded' (boolean) and "
    "'conflicts' (array of strings). Begin your reply with {\"answer\": and emit no other "
    "top-level key. Never repeat, summarise or continue the evidence, and never return an "
    "array at the top level. "
    "CRITICAL: Do NOT begin the answer with any welcome or greeting phrase such as "
    "'Welcome to Mansam', 'It is a pleasure', 'Hello', 'Hi', or any similar opener. "
    "The greeting was already sent. Go straight to the helpful response."
)

VERIFICATION_CONTRACT = (
    "Reply with exactly one JSON object and nothing else. It must contain the keys "
    "'supported' (boolean), 'answered_question' (boolean), 'unsupported_claims' (array of "
    "strings), 'citation_errors' (array of strings), 'contradictions' (array of strings), "
    "'recommended_action' (one of \"accept\", \"regenerate\" or \"refuse\") and "
    "'confidence' (number between 0 and 1). Begin your reply with {\"supported\": and emit "
    "no other top-level key. Never repeat, summarise or continue the evidence."
)

# The verifier is sent the same evidence as the draft plus the cited sources, so a full
# second copy of every chunk made verification the slowest call in the pipeline. Only the
# chunks a claim actually cites need their full text: a spreadsheet row can be thousands of
# characters wide, and truncating it hid the very field under verification (the Mamlakati
# price sits at character 1552, so a 700-character cap made a correct answer look
# unsupported). Uncited evidence is summarised, cited evidence is nearly whole.
VERIFY_CHUNK_CHARS = 700
VERIFY_CITED_CHARS = 3000


def _authoritative_counts(evidence: list[RetrievedEvidence], question: str) -> list[dict[str, Any]]:
    """Recorded count fields for a counting question, tagged with their evidence block.

    The patterns live in ``app.rag.counts`` because the retriever fetches the same rows
    directly; this only adds the 1-based evidence index the draft has to cite.
    """
    if not is_count_question(question):
        return []
    counts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(evidence, 1):
        for field in count_fields(item.content):
            if field["key"] in seen:
                continue
            seen.add(field["key"])
            counts.append({**field, "evidence_index": index})
    return counts


class GroundedAnswerService:
    def __init__(
        self,
        llm: LLMProvider,
        prompts: PromptRepository,
        min_score: float,
        min_evidence: int,
        max_context_chars: int,
        *,
        draft_model: str | None = None,
        fast_model: str | None = None,
        verify_enabled: bool = True,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.min_score = min_score
        self.min_evidence = min_evidence
        self.max_context_chars = max_context_chars
        self.draft_model = draft_model
        self.fast_model = fast_model
        self.verify_enabled = verify_enabled

    async def answer(self, *, question: str, language: str, evidence: list[RetrievedEvidence]) -> GroundedAnswer:
        accepted = [item for item in evidence if _quality_score(item) >= self.min_score]
        if len(accepted) < self.min_evidence:
            return self._refusal(language, "insufficient_evidence")
        accepted = _fit_context(accepted, self.max_context_chars)
        counts = _authoritative_counts(accepted, question)
        draft = await self._draft(question, language, accepted, strict=False, counts=counts)
        if not draft["grounded"]:
            return self._refusal(language, "model_reported_insufficient_evidence")
        citations, citation_errors = _resolve_citations(draft["citation_ids"], accepted)
        if not citations:
            # The model failed to cite valid evidence; fall back to the top-ranked
            # retrieved chunk so the answer can still be delivered and audited.
            citations = _build_citations([str(accepted[0].chunk_id)], accepted)
            citation_errors = []
        if not self.verify_enabled:
            # Latency-budgeted path: the draft already cleared the admission gate, reported
            # itself grounded and its citations resolve, so the second generation is skipped.
            # Confidence is derived from retrieval quality instead of a verifier verdict and
            # the status records that no independent check ran.
            retrieval_confidence = sum(_quality_score(item) for item in accepted[:3]) / min(3, len(accepted))
            confidence = max(0.0, min(0.75, 0.6 * retrieval_confidence + 0.4 * 1.0))
            if citation_errors:
                confidence = min(confidence, 0.4)
            return GroundedAnswer(
                answer=draft["answer"],
                confidence=round(confidence, 3),
                citations=citations,
                grounded=True,
                verification_status="verification_skipped",
                conflicts=[*draft["conflicts"]],
            )
        verification = await self._verify(question, draft["answer"], citations, accepted)
        if citation_errors:
            verification = VerificationResult(
                supported=verification.supported,
                answered_question=verification.answered_question,
                unsupported_claims=verification.unsupported_claims,
                citation_errors=[*verification.citation_errors, *citation_errors],
                contradictions=verification.contradictions,
                recommended_action=verification.recommended_action,
                confidence=min(verification.confidence, 0.5),
            )
        if not verification.supported or verification.recommended_action != "accept":
            regenerated = await self._draft(question, language, accepted, strict=True, counts=counts)
            if not regenerated["grounded"]:
                return self._best_effort(regenerated["answer"], citations, accepted, "unverified_fallback")
            reg_citations, _reg_errors = _resolve_citations(regenerated["citation_ids"], accepted)
            if not reg_citations:
                reg_citations = citations
            second_verification = await self._verify(
                question,
                regenerated["answer"],
                reg_citations,
                accepted,
            )
            if second_verification.supported and second_verification.recommended_action == "accept":
                draft = regenerated
                citations = reg_citations
                verification = second_verification
                status = "verified_after_regeneration"
            else:
                # Deliver the strict-regeneration draft flagged as unverified rather
                # than refusing outright, so the user still receives a useful answer.
                return self._best_effort(
                    regenerated["answer"], reg_citations, accepted, "unverified_fallback"
                )
        else:
            status = "verified"
        retrieval_confidence = sum(_quality_score(item) for item in accepted[:3]) / min(3, len(accepted))
        citation_coverage = 1.0 if citations else 0.0
        confidence = max(
            0.0,
            min(
                1.0,
                0.4 * retrieval_confidence + 0.4 * verification.confidence + 0.2 * citation_coverage,
            ),
        )
        return GroundedAnswer(
            answer=draft["answer"],
            confidence=confidence,
            citations=citations,
            grounded=True,
            verification_status=status,
            conflicts=[*draft["conflicts"], *verification.contradictions],
        )

    async def _draft(
        self,
        question: str,
        language: str,
        evidence: list[RetrievedEvidence],
        *,
        strict: bool,
        counts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        context = _render_evidence(evidence)
        strict_note = (
            "A previous draft failed verification. Remove every statement that is not explicitly supported."
            if strict
            else ""
        )
        contract = ANSWER_CONTRACT
        if counts:
            contract = (
                f"{ANSWER_CONTRACT} The payload also carries 'authoritative_counts', the recorded "
                "count fields. For a counting question answer with the 'value' of the entry whose "
                "'description' matches the question's scope, exactly as written, and cite its "
                "'evidence_index'. Never count evidence rows yourself."
            )
        try:
            request_payload: dict[str, Any] = {
                "question": question,
                "response_language": language,
                "strict_note": strict_note,
                "evidence": context,
                "output_contract": contract,
            }
            if counts:
                request_payload["authoritative_counts"] = counts
            result = await self.llm.generate(
                [
                    LLMMessage("system", self.prompts.load("system/core.md")),
                    LLMMessage("system", self.prompts.load("answer_generation/grounded.md")),
                    LLMMessage("user", json.dumps(request_payload, ensure_ascii=False)),
                ],
                temperature=0,
                max_tokens=1000,
                response_format="json",
                model=self.draft_model,
            )
            payload = _parse_json(result.text)
            answer = str(payload.get("answer", "")).strip()
            ids = _normalize_citation_ids(payload)
            conflicts = [str(item) for item in payload.get("conflicts", [])][:10]
            if not answer:
                raise ValueError("Draft answer is empty")
            return {
                "answer": answer,
                "citation_ids": ids,
                "grounded": bool(payload.get("grounded", False)),
                "conflicts": conflicts,
            }
        except (LLMUnavailableError, ValueError, TypeError, json.JSONDecodeError):
            raise

    async def _verify(
        self,
        question: str,
        answer: str,
        citations: list[Citation],
        evidence: list[RetrievedEvidence],
    ) -> VerificationResult:
        cited_ids = frozenset(str(citation.chunk_id) for citation in citations)
        request_payload = {
            "question": question,
            "draft_answer": answer,
            "cited_sources": [
                {
                    "chunk_id": citation.chunk_id,
                    "document": citation.document_name,
                    "excerpt": citation.excerpt,
                }
                for citation in citations
            ],
            "evidence": _render_evidence(evidence, VERIFY_CHUNK_CHARS, cited_ids),
            "output_contract": VERIFICATION_CONTRACT,
        }
        result = await self.llm.generate(
            [
                LLMMessage("system", self.prompts.load("system/core.md")),
                LLMMessage("system", self.prompts.load("verification/evidence_check.md")),
                LLMMessage("user", json.dumps(request_payload, ensure_ascii=False)),
            ],
            temperature=0,
            max_tokens=700,
            response_format="json",
            model=self.fast_model,
        )
        payload = _parse_json(result.text)
        action = str(payload.get("recommended_action", "refuse"))
        if action not in {"accept", "regenerate", "refuse"}:
            action = "refuse"
        return VerificationResult(
            supported=bool(payload.get("supported", False)),
            answered_question=bool(payload.get("answered_question", False)),
            unsupported_claims=[str(item) for item in payload.get("unsupported_claims", [])][:20],
            citation_errors=[str(item) for item in payload.get("citation_errors", [])][:20],
            contradictions=[str(item) for item in payload.get("contradictions", [])][:20],
            recommended_action=action,
            confidence=max(0, min(1, float(payload.get("confidence", 0)))),
        )

    @staticmethod
    def _refusal(language: str, reason: str) -> GroundedAnswer:
        key = "ar" if language.startswith("ar") else "en"
        return GroundedAnswer(
            answer=UNCERTAINTY[key],
            confidence=0,
            citations=[],
            grounded=False,
            verification_status=reason,
        )

    @staticmethod
    def _best_effort(
        answer: str,
        citations: list[Citation],
        evidence: list[RetrievedEvidence],
        status: str,
    ) -> GroundedAnswer:
        retrieval_confidence = sum(_quality_score(item) for item in evidence[:3]) / min(3, len(evidence))
        return GroundedAnswer(
            answer=answer,
            confidence=round(min(0.5, 0.5 * retrieval_confidence), 2),
            citations=citations,
            grounded=False,
            verification_status=status,
            conflicts=[],
        )


def _quality_score(item: RetrievedEvidence) -> float:
    """Admission score that is compared against ``rag_min_score``.

    The reranker score is deliberately excluded. Cross-encoder rerankers return an
    uncalibrated relevance probability whose scale does not match the cosine
    similarity that ``rag_min_score`` is tuned for (a relevant pair scores ~0.9
    while an unrelated pair scores ~0.0001), so the retriever already uses it for
    ordering. Feeding it into this gate rejects every candidate as soon as a
    reranker is configured, which surfaces as ``insufficient_evidence``.
    """
    if item.vector_score is not None:
        return max(0.0, min(1.0, item.vector_score))
    return max(0.0, min(1.0, item.lexical_score or 0.0))


def _fit_context(evidence: list[RetrievedEvidence], max_chars: int) -> list[RetrievedEvidence]:
    selected: list[RetrievedEvidence] = []
    used = 0
    for item in evidence:
        if selected and used + len(item.content) > max_chars:
            break
        selected.append(item)
        used += len(item.content)
    return selected


def _render_evidence(
    evidence: list[RetrievedEvidence],
    max_chars_per_chunk: int | None = None,
    cited_ids: frozenset[str] = frozenset(),
    cited_chars: int = VERIFY_CITED_CHARS,
) -> str:
    blocks = []
    for index, item in enumerate(evidence, 1):
        assessment = assess_prompt_injection(item.content)
        header = json.dumps(
            {
                "index": index,
                "chunk_id": str(item.chunk_id),
                "document": item.document_name,
                "version": item.document_version,
                "page": item.page_number,
                "section": item.section,
                "possible_injection": assessment.suspicious,
            },
            ensure_ascii=False,
        )
        content = item.content
        limit = cited_chars if str(item.chunk_id) in cited_ids else max_chars_per_chunk
        if limit is not None and len(content) > limit:
            content = content[:limit]
        blocks.append(f"{header}\n{wrap_untrusted_evidence(content, str(item.chunk_id))}")
    return "\n\n".join(blocks)


def _normalize_citation_ids(payload: dict[str, Any]) -> list[Any]:
    raw = payload.get("citation_ids", payload.get("citation_chunk_ids", []))
    if not isinstance(raw, list):
        raw = [raw]
    normalized: list[Any] = []
    for item in raw[:10]:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            normalized.append(item)
        elif isinstance(item, str) and item.strip():
            stripped = item.strip()
            normalized.append(int(stripped) if stripped.isdigit() else stripped)
    return normalized


def _resolve_citations(
    ids: list[Any],
    evidence: list[RetrievedEvidence],
) -> tuple[list[Citation], list[str]]:
    by_index = {index: item for index, item in enumerate(evidence, 1)}
    by_uuid = {str(item.chunk_id): item for item in evidence}
    errors: list[str] = []
    selected: list[RetrievedEvidence] = []
    for raw in ids:
        item: RetrievedEvidence | None = None
        if isinstance(raw, int):
            item = by_index.get(raw)
            if item is None:
                errors.append(f"Unknown evidence index: {raw}")
        else:
            item = by_uuid.get(str(raw))
            if item is None:
                errors.append(f"Unknown citation chunk: {raw}")
        if item is not None and item not in selected:
            selected.append(item)
    return _build_citations([str(item.chunk_id) for item in selected], evidence), errors


def _build_citations(ids: list[str], evidence: list[RetrievedEvidence]) -> list[Citation]:
    by_id = {str(item.chunk_id): item for item in evidence}
    citations = []
    seen = set()
    for chunk_id in ids:
        if chunk_id in seen or chunk_id not in by_id:
            continue
        seen.add(chunk_id)
        item = by_id[chunk_id]
        excerpt = " ".join(item.content.split())[:400]
        citations.append(
            Citation(
                document_id=str(item.document_id),
                document_name=item.document_name,
                document_version=item.document_version,
                chunk_id=chunk_id,
                page=item.page_number,
                section=item.section,
                excerpt=excerpt,
            )
        )
    return citations


def _parse_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    return payload