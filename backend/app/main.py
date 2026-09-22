import asyncio
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.api import api_router
from app.api.routes.health import router as health_router
from app.config import get_settings
from app.dependencies import get_http_client, get_redis
from app.embeddings import HuggingFaceEmbeddingProvider
from app.llm import HuggingFaceLLMProvider, LLMMessage
from app.monitoring.logging import configure_logging, get_logger
from app.monitoring.metrics import REQUEST_COUNT, REQUEST_LATENCY

settings = get_settings()
configure_logging()
logger = get_logger(__name__)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    openapi_url=f"{settings.api_prefix}/openapi.json",
    docs_url=f"{settings.api_prefix}/docs",
    redoc_url=f"{settings.api_prefix}/redoc",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)
app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Landing page for the bare service URL.

    The UI is a separate static site; browsers hitting the API root otherwise
    get FastAPI's default `{"detail": "Not Found"}`. Point at the real
    entry points instead.
    """
    return {
        "service": settings.app_name,
        "docs": f"{settings.api_prefix}/docs",
        "health": "/health/live",
        "ready": "/health/ready",
    }


@app.on_event("startup")
async def validate_configuration() -> None:
    settings.validate_runtime()
    if settings.keep_model_warm:
        app.state.warmup_task = asyncio.create_task(_keep_model_warm())


@app.on_event("shutdown")
async def stop_background_tasks() -> None:
    task = getattr(app.state, "warmup_task", None)
    if task is not None:
        task.cancel()


async def _keep_model_warm() -> None:
    """Hold the answer model and the embedding model in memory.

    Ollama unloads a model after five minutes of inactivity, and neither a per-request
    ``keep_alive`` nor the ``OLLAMA_KEEP_ALIVE`` variable changes that through its
    OpenAI-compatible endpoint (both were measured). Warming only the answer model was not
    enough: on a 6 GB GPU the first retrieval then had to load the embedding model next to a
    model already holding the card, and that load failed - reproducibly in the seconds after
    startup and not once the card had settled. Each tick therefore warms both, retries a few
    times with a short backoff, and costs a few hundred milliseconds when it succeeds.
    """
    if not settings.hf_inference_url:
        logger.info("model_warmup_skipped", reason="no LLM endpoint configured")
        return
    while True:
        await _warm_models()
        await asyncio.sleep(settings.keep_warm_interval_seconds)


async def _warm_models(attempts: int = 3, retry_seconds: int = 15) -> None:
    for attempt in range(1, attempts + 1):
        failures: list[str] = []
        try:
            llm = HuggingFaceLLMProvider(settings, get_http_client())
            await llm.generate([LLMMessage("user", "Reply with the single word: ok")], max_tokens=1)
        except Exception as exc:
            failures.append(f"llm:{type(exc).__name__}:{exc}")
        if settings.embedding_inference_url:
            try:
                embeddings = HuggingFaceEmbeddingProvider(settings, get_http_client())
                await embeddings.embed_query("warm")
            except Exception as exc:
                failures.append(f"embedding:{type(exc).__name__}:{exc}")
        if not failures:
            logger.info(
                "models_kept_warm",
                model=settings.hf_model,
                embedding_model=settings.embedding_model,
                interval_seconds=settings.keep_warm_interval_seconds,
            )
            return
        logger.warning("model_warmup_failed", attempt=attempt, attempts=attempts, failures=failures)
        if attempt < attempts:
            await asyncio.sleep(retry_seconds)


@app.middleware("http")
async def request_context(request: Request, call_next):
    clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    bind_contextvars(request_id=request_id)
    started = time.perf_counter()
    path = request.url.path
    try:
        if (
            request.method != "OPTIONS"
            and path.startswith(settings.api_prefix)
            and not path.endswith(("/docs", "/openapi.json"))
        ):
            redis = get_redis()
            client = request.client.host if request.client else "unknown"
            minute = int(time.time() // 60)
            key = f"rate:{client}:{minute}"
            try:
                count = await redis.incr(key)
                if count == 1:
                    await redis.expire(key, 70)
                if count > settings.rate_limit_per_minute:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Rate limit exceeded", "request_id": request_id},
                        headers={"Retry-After": "60", **_cors_headers(request)},
                    )
            except RedisError:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "Rate limiting service unavailable",
                        "request_id": request_id,
                    },
                    headers=_cors_headers(request),
                )
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled_request_error", method=request.method, path=path)
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    duration = time.perf_counter() - started
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    REQUEST_COUNT.labels(request.method, path, response.status_code).inc()
    REQUEST_LATENCY.labels(request.method, path).observe(duration)
    logger.info(
        "http_request",
        method=request.method,
        path=path,
        status=response.status_code,
        duration_ms=int(duration * 1000),
        # "direct" identifies a script or curl; a browser sends its origin, which makes it
        # possible to tell a slow UI apart from a slow pipeline when reading this log.
        caller=request.headers.get("origin", "direct"),
    )
    return response


def _cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("Origin")
    if origin and origin in settings.frontend_origins:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
