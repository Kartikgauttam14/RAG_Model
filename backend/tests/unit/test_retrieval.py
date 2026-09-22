from types import SimpleNamespace
from uuid import uuid4

from app.retrieval.hybrid import DEFAULT_LEXICAL_CONFIG, HybridRetriever
from sqlalchemy.dialects import postgresql


def _retriever(lexical_config: str = DEFAULT_LEXICAL_CONFIG) -> HybridRetriever:
    return HybridRetriever(
        # The lexical SQL is what these tests exercise, so no embedding provider or
        # reranker is needed; the retriever only touches them in the vector stage.
        embedding_provider=None,  # type: ignore[arg-type]
        reranker=None,
        vector_top_k=20,
        lexical_top_k=20,
        rerank_top_k=8,
        lexical_config=lexical_config,
    )


def _lexical_query_text(query: str, lexical_config: str = DEFAULT_LEXICAL_CONFIG) -> str:
    compiled = _retriever(lexical_config)._lexical_tsquery(query).compile(dialect=postgresql.dialect())
    return str(compiled.params["websearch_to_tsquery_2"])


def _row(chunk_id, content: str, score: float, document_name: str = "Policy"):
    chunk = SimpleNamespace(
        id=chunk_id,
        document_id=uuid4(),
        content=content,
        page_number=2,
        section="Delivery",
        metadata_json={},
    )
    return (chunk, document_name, 1, score)


def test_rrf_merge_deduplicates_overlapping_vector_and_lexical_candidates() -> None:
    shared_id = uuid4()
    vector_only_id = uuid4()
    lexical_only_id = uuid4()

    merged = HybridRetriever._rrf_merge(
        [_row(shared_id, "shared", 0.9), _row(vector_only_id, "vector", 0.8)],
        [_row(shared_id, "shared", 0.7), _row(lexical_only_id, "lexical", 0.6)],
    )

    assert [item.chunk_id for item in merged].count(shared_id) == 1
    shared = next(item for item in merged if item.chunk_id == shared_id)
    assert shared.vector_score == 0.9
    assert shared.lexical_score == 0.7
    assert shared.fused_score > next(item for item in merged if item.chunk_id == vector_only_id).fused_score


def test_lexical_query_uses_or_joined_terms_so_any_term_matches() -> None:
    query = _lexical_query_text("how many mansam boutiques are there in saudi arabia", "simple")

    assert query == "how or many or mansam or boutiques or are or there or in or saudi or arabia"


def test_lexical_query_uses_the_configured_text_search_configuration() -> None:
    assert _lexical_query_text("boutiques", "english") == "boutiques"
    assert _lexical_query_text("boutiques", "simple") == "boutiques"


def test_lexical_query_falls_back_to_raw_query_when_no_terms_are_extractable() -> None:
    assert _lexical_query_text("!!! ???", "english") == "!!! ???"


def test_rrf_merge_preserves_provenance_for_single_source_candidates() -> None:
    chunk_id = uuid4()

    merged = HybridRetriever._rrf_merge([_row(chunk_id, "evidence", 0.5)], [])

    assert len(merged) == 1
    assert merged[0].document_name == "Policy"
    assert merged[0].document_version == 1
    assert merged[0].content == "evidence"
    assert merged[0].lexical_score is None
