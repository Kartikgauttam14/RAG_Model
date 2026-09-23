import asyncio

import httpx
from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import get_redis
from app.llm import LLMMessage, LLMUnavailableError
from app.llm.huggingface import HuggingFaceLLMProvider
from app.monitoring.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

# Localhost LLM/embedding URLs work on a laptop (Ollama) but mean "this
# container itself" on Render, where they fail as connection-refused.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict:
    database_ready, redis_ready, vector_store, llm = await asyncio.gather(
        _check_database(db),
        _check_redis(redis),
        _check_vector_store(settings),
        _check_llm(settings),
    )
    checks = {"database": database_ready, "redis": redis_ready}
    if vector_store is not None:
        checks["vector_store"] = vector_store
    if llm is not None:
        checks["llm"] = llm
    core_ready = database_ready and redis_ready
    if not core_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if core_ready else "not_ready", "checks": checks}


async def _check_database(db: AsyncSession) -> bool:
    try:
        await db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("readiness_database_failed", error_type=type(exc).__name__)
        return False


async def _check_redis(redis: Redis) -> bool:
    try:
        return bool(await redis.ping())
    except Exception as exc:
        logger.warning("readiness_redis_failed", error_type=type(exc).__name__)
        return False


async def _check_vector_store(settings: Settings) -> bool | None:
    """Report Qdrant health when it is the selected backend; None skips the check.

    The check is best-effort and read-only (collection lookup). Retrieval
    already falls back to pgvector on outage, so a down Qdrant must not flip
    the whole API to not-ready: it reports False as a signal while the core
    checks keep their vote.
    """
    if settings.vector_store_backend != "qdrant" or not settings.qdrant_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.qdrant_timeout_seconds) as client:
            headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
            response = await client.get(
                f"{settings.qdrant_url.rstrip('/')}/collections/{settings.qdrant_collection}",
                headers=headers,
            )
            return response.status_code == 200
    except Exception as exc:
        logger.warning("readiness_vector_store_failed", error_type=type(exc).__name__)
        return False


async def _check_llm(settings: Settings) -> dict | None:
    """Probe the generation endpoint so Render misconfiguration is visible.

    Returns a small status dict (or None when no LLM URL is configured at
    all). Like the vector-store check this is advisory: it never flips the
    overall readiness vote, because the LLM can legitimately cold-start or be
    momentarily throttled while the API itself is healthy. The dict carries a
    machine-readable ``reason`` matching ``LLMFailureReason`` plus a ``hint``
    naming the setting to fix, and warns immediately about loopback URLs,
    which are the #1 Render failure (Ollama URL copied to the cloud).
    """
    if not settings.hf_inference_url:
        return None
    lowered = settings.hf_inference_url.lower()
    if any(host in lowered for host in _LOOPBACK_HOSTS):
        logger.warning("readiness_llm_loopback_url", endpoint=settings.hf_inference_url)
        return {
            "ok": False,
            "reason": "unreachable_endpoint",
            "hint": "HF_INFERENCE_URL points at localhost, which is this container on Render — "
            "set it to the hosted base URL (e.g. https://router.huggingface.co).",
        }
    try:
        provider = HuggingFaceLLMProvider(settings)
        await provider.generate(
            [LLMMessage("user", "Reply with: ok")],
            temperature=0,
            max_tokens=5,
        )
    except LLMUnavailableError as exc:
        logger.warning("readiness_llm_failed", reason=exc.reason)
        return {"ok": False, "reason": exc.reason, "hint": str(exc)}
    except Exception as exc:  # defensive: a probe must never break /health/ready
        logger.warning("readiness_llm_failed", error_type=type(exc).__name__)
        return {"ok": False, "reason": "provider_error", "hint": str(exc)}
    return {"ok": True, "reason": "ok", "model": settings.hf_model}
