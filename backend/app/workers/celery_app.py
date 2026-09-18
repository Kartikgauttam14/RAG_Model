from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "mansam_rag",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.ingestion"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1700,
    broker_connection_retry_on_startup=True,
)
celery_app.autodiscover_tasks(["app.workers"])
