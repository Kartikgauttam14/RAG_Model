import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_current_principal
from app.chat import ChatRequest, ChatResponse, ChatService
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import get_embeddings, get_llm, get_prompts, get_redis, get_reranker
from app.embeddings import EmbeddingProvider
from app.llm import LLMProvider, LLMUnavailableError
from app.memory import MemoryService
from app.monitoring.logging import get_logger
from app.rag.prompts import PromptRepository
from app.reranking import Reranker
from app.retrieval import HybridRetriever

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


def _service(
    settings: Settings,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
    reranker: Reranker | None,
    redis: Redis,
    prompts: PromptRepository,
) -> ChatService:
    retriever = HybridRetriever(
        embeddings,
        reranker,
        settings.rag_vector_top_k,
        settings.rag_lexical_top_k,
        settings.rerank_top_k,
        lexical_config=settings.lexical_text_search_config,
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
) -> ChatResponse:
    request_id = str(uuid.uuid4())
    service = _service(settings, llm, embeddings, reranker, redis, prompts)
    try:
        return await service.respond(db, principal, payload, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Language model is unavailable") from exc


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
) -> StreamingResponse:
    request_id = str(uuid.uuid4())
    service = _service(settings, llm, embeddings, reranker, redis, prompts)

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
                await queue.put(
                    {
                        "type": "error",
                        "error": "service_unavailable" if isinstance(exc, LLMUnavailableError) else "request_failed",
                    }
                )
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
