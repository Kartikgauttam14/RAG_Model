import asyncio
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_current_principal
from app.chat import ChatRequest, ChatResponse, ChatService
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import get_embeddings, get_http_client, get_llm, get_prompts, get_redis, get_reranker
from app.embeddings import EmbeddingProvider
from app.llm import LLMProvider, LLMUnavailableError
from app.memory import MemoryService
from app.monitoring.logging import get_logger
from app.rag.prompts import PromptRepository
from app.reranking import Reranker
from app.retrieval import HybridRetriever
from app.vectorstore import QdrantVectorStore, VectorStore

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


def get_vector_store(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> VectorStore | None:
    """Return the dedicated dense index, or None when pgvector serves the vector arm."""
    if settings.vector_store_backend != "qdrant":
        return None
    try:
        return QdrantVectorStore(settings, client)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant endpoint is not configured",
        ) from exc


def _service(
    settings: Settings,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
    reranker: Reranker | None,
    redis: Redis,
    prompts: PromptRepository,
    vector_store: VectorStore | None = None,
) -> ChatService:
    retriever = HybridRetriever(
        embeddings,
        reranker,
        settings.rag_vector_top_k,
        settings.rag_lexical_top_k,
        settings.rerank_top_k,
        lexical_config=settings.lexical_text_search_config,
        vector_store=vector_store,
    )
    memory = MemoryService(redis, embeddings, llm, prompts, settings)
    return ChatService(llm=llm, retriever=retriever, memory=memory, prompts=prompts, settings=settings)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    llm: LLMProvider = Depends(get_llm),
    embeddings: EmbeddingProvider = Depends(get_embeddings),
    reranker: Reranker | None = Depends(get_reranker),
    redis: Redis = Depends(get_redis),
    prompts: PromptRepository = Depends(get_prompts),
    vector_store: VectorStore | None = Depends(get_vector_store),
) -> ChatResponse:
    request_id = str(uuid.uuid4())
    service = _service(settings, llm, embeddings, reranker, redis, prompts, vector_store)
    try:
        return await service.respond(db, principal, payload, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=_llm_error_detail(exc),
        ) from exc


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    llm: LLMProvider = Depends(get_llm),
    embeddings: EmbeddingProvider = Depends(get_embeddings),
    reranker: Reranker | None = Depends(get_reranker),
    redis: Redis = Depends(get_redis),
    prompts: PromptRepository = Depends(get_prompts),
    vector_store: VectorStore | None = Depends(get_vector_store),
) -> StreamingResponse:
    request_id = str(uuid.uuid4())
    service = _service(settings, llm, embeddings, reranker, redis, prompts, vector_store)

    async def events():
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def progress(state: str) -> None:
            await queue.put({"type": "status", "state": state, "request_id": request_id})

        async def run() -> None:
            try:
                response = await service.respond(db, principal, payload, request_id, progress)
                await queue.put({"type": "answer", "data": response.model_dump(mode="json")})
            except Exception as exc:
                logger.exception(
                    "chat_stream_failed",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                )
                if isinstance(exc, LLMUnavailableError):
                    await queue.put(
                        {
                            "type": "error",
                            "error": "service_unavailable",
                            "reason": exc.reason,
                            "detail": _llm_error_detail(exc),
                        }
                    )
                else:
                    await queue.put({"type": "error", "error": "request_failed"})
            finally:
                await queue.put({"type": "end"})

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item["type"] == "end":
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream")


def _llm_error_detail(exc: LLMUnavailableError) -> str:
    """User-facing message naming the cause plus the one setting to fix.

    The UI shows this string, so it stays short and actionable; the full
    endpoint/model/status context remains in the `llm_request_failed` log.
    """
    hints = {
        "unreachable_endpoint": (
            "The language model endpoint is unreachable — check HF_INFERENCE_URL "
            "(a localhost Ollama URL does not work on Render)."
        ),
        "unauthorized": "The language model rejected the credentials — check HF_TOKEN.",
        "quota_exhausted": ("The language model has no remaining credit — check provider billing/quota and try again."),
        "model_not_found": (
            "The language model id is not served at this URL — check HF_MODEL "
            "(Ollama tags like gemma3:4b only work locally)."
        ),
        "rate_limited": "The language model is throttled — wait a minute and try again.",
        "timeout": "The language model timed out — it may be cold-loading; try again.",
        "empty_completion": "The language model returned an empty reply — try again.",
        "provider_error": "The language model is unavailable right now — please try again.",
    }
    return hints.get(exc.reason, str(exc) or hints["provider_error"])
