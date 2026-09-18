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
from app.dependencies import get_redis
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


@app.on_event("startup")
async def validate_configuration() -> None:
    settings.validate_runtime()


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
