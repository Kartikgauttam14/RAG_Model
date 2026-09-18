from functools import lru_cache

import httpx
from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.embeddings import HuggingFaceEmbeddingProvider
from app.llm import HuggingFaceLLMProvider
from app.rag.prompts import PromptRepository
from app.reranking import HuggingFaceReranker
from app.speech import HuggingFaceSTTProvider, HuggingFaceTTSProvider


@lru_cache
def get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def get_prompts() -> PromptRepository:
    return PromptRepository()


def get_llm(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> HuggingFaceLLMProvider:
    try:
        return HuggingFaceLLMProvider(settings, client)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hosted LLM endpoint is not configured",
        ) from exc


def get_embeddings(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> HuggingFaceEmbeddingProvider:
    try:
        return HuggingFaceEmbeddingProvider(settings, client)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding endpoint is not configured",
        ) from exc


def get_reranker(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> HuggingFaceReranker | None:
    if not settings.rerank_inference_url:
        return None
    return HuggingFaceReranker(settings, client)


def get_stt(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> HuggingFaceSTTProvider:
    try:
        return HuggingFaceSTTProvider(settings, client)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Speech recognition is not configured") from exc


def get_tts(
    settings: Settings = Depends(get_settings), client: httpx.AsyncClient = Depends(get_http_client)
) -> HuggingFaceTTSProvider:
    try:
        return HuggingFaceTTSProvider(settings, client)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Speech synthesis is not configured") from exc
