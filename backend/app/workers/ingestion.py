import asyncio
import re
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import delete, func

from app.config import get_settings
from app.database.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobStatus,
)
from app.database.session import SessionFactory
from app.embeddings import HuggingFaceEmbeddingProvider
from app.ingestion import StructureAwareChunker, extract_document
from app.ingestion.ocr import HTTPCloudOCRProvider
from app.monitoring.logging import get_logger
from app.vectorstore import QdrantVectorStore, VectorStoreUnavailableError, point_payload
from app.vectorstore.base import VectorPoint
from app.workers.celery_app import celery_app

logger = get_logger(__name__)
ARABIC = re.compile(r"[\u0600-\u06ff]")
_LOOP: asyncio.AbstractEventLoop | None = None
T = TypeVar("T")


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run a task coroutine on one event loop per worker process.

    ``asyncio.run`` would build and close a loop for every task, but pooled asyncpg connections stay
    bound to the loop that created them, so the second task in a worker reuses dead connections and
    fails with ``AttributeError("'NoneType' object has no attribute 'send'")``.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@celery_app.task(bind=True, max_retries=3, name="ingestion.index_document")
def index_document(self, job_id: str) -> dict[str, int | str]:
    try:
        return _run_async(_index_document(uuid.UUID(job_id)))
    except Exception as exc:
        countdown = min(300, 2**self.request.retries * 10)
        raise self.retry(exc=exc, countdown=countdown) from exc


async def _index_document(job_id: uuid.UUID) -> dict[str, int | str]:
    settings = get_settings()
    async with SessionFactory() as db:
        job = await db.get(IngestionJob, job_id)
        if not job or job.status == JobStatus.cancelled:
            return {"status": "cancelled", "chunks": 0}
        document = await db.get(Document, job.document_id)
        version = await db.get(DocumentVersion, job.document_version_id)
        if not document or not version or not version.storage_path:
            await _fail(db, job, document, "missing_source", "Source file or version is missing")
            return {"status": "failed", "chunks": 0}
        job.status = JobStatus.processing
        job.stage = "extracting"
        job.attempts += 1
        document.status = DocumentStatus.indexing
        await db.commit()
        try:
            data = Path(version.storage_path).read_bytes()
            extracted = extract_document(
                data,
                document.name,
                document.media_type,
                settings.ingestion_excluded_sheets,
                settings.ingestion_workflow_columns,
                settings.ingestion_unpublished_markers,
            )
            if extracted.needs_ocr:
                if settings.ocr_provider == "disabled":
                    raise RuntimeError("Scanned PDF requires a configured OCR provider")
                extracted = await HTTPCloudOCRProvider(settings).extract_pdf(data, document.name)
            job.stage = "chunking"
            job.progress = 0.25
            await db.commit()
            chunker = StructureAwareChunker(
                settings.chunk_target_chars,
                settings.chunk_max_chars,
                settings.chunk_overlap_chars,
            )
            chunks = chunker.chunk(extracted)
            if not chunks:
                raise RuntimeError("No usable chunks were produced")
            job.stage = "embedding"
            job.progress = 0.40
            await db.commit()
            embeddings = HuggingFaceEmbeddingProvider(settings)
            vectors: list[list[float]] = []
            batch_size = 32
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start : start + batch_size]
                vectors.extend(await embeddings.embed_documents([chunk.content for chunk in batch]))
                job.progress = 0.40 + 0.45 * min(1, (start + len(batch)) / len(chunks))
                await db.commit()
            await db.execute(delete(DocumentChunk).where(DocumentChunk.document_version_id == version.id))
            stored: list[DocumentChunk] = []
            for chunk, vector in zip(chunks, vectors, strict=True):
                language = _detect_language(chunk.content)
                stored.append(
                    DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        tenant_id=document.tenant_id,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        content_hash=chunk.content_hash,
                        section=chunk.section,
                        page_number=chunk.page_number,
                        language=language,
                        category=version.extracted_metadata.get("category"),
                        tags=version.extracted_metadata.get("tags", []),
                        access_scope=document.access_scope,
                        metadata_json={**chunk.metadata, "embedding_model": settings.embedding_model},
                        embedding=vector,
                        search_vector=func.to_tsvector(settings.lexical_text_search_config, chunk.content),
                    )
                )
            db.add_all(stored)
            await db.flush()
            # Dual-write the dense copy: Postgres rows flush first so chunk ids
            # exist; Qdrant points are addressed by those ids. A Qdrant outage
            # must not fail ingestion — Postgres stays queryable via pgvector
            # and the next reindex repairs the Qdrant copy (delete+upsert).
            await _mirror_to_vector_store(settings, version, stored, vectors)
            version.extracted_metadata = {**version.extracted_metadata, **extracted.metadata}
            document.status = DocumentStatus.ready
            job.status = JobStatus.completed
            job.stage = "completed"
            job.progress = 1
            await db.commit()
            logger.info("ingestion_completed", job_id=str(job.id), chunks=len(chunks))
            return {"status": "completed", "chunks": len(chunks)}
        except Exception as exc:
            await _fail(db, job, document, type(exc).__name__, str(exc))
            logger.exception("ingestion_failed", job_id=str(job.id), error_type=type(exc).__name__)
            raise


async def _fail(db, job: IngestionJob, document: Document | None, code: str, message: str) -> None:
    job.status = JobStatus.failed
    job.stage = "failed"
    job.error_code = code[:100]
    job.error_message = message[:4000]
    if document:
        document.status = DocumentStatus.failed
    await db.commit()


def _detect_language(text: str) -> str:
    sample = text[:2000]
    arabic = len(ARABIC.findall(sample))
    letters = sum(character.isalpha() for character in sample)
    return "ar" if letters and arabic / letters > 0.3 else "en"


async def _mirror_to_vector_store(
    settings: Any,
    version: DocumentVersion,
    stored: list[DocumentChunk],
    vectors: list[list[float]],
) -> None:
    """Dual-write chunk vectors to the dedicated dense index (best effort).

    Postgres rows are flushed before this call, so every chunk has an id to
    address its Qdrant point. The old version copy is deleted first so a
    reindex never leaves stale points behind. Any outage is logged and
    swallowed: ingestion still completes on Postgres/pgvector.
    """
    if settings.vector_store_backend != "qdrant" or not stored:
        return
    try:
        store = QdrantVectorStore(settings)
        await store.ensure_collection()
        await store.delete_by_version(document_version_id=str(version.id))
        await store.upsert(
            [
                VectorPoint(
                    point_id=str(chunk.id),
                    vector=vector,
                    payload=point_payload(
                        tenant_id=chunk.tenant_id,
                        access_scope=chunk.access_scope,
                        document_id=str(chunk.document_id),
                        document_version_id=str(chunk.document_version_id),
                        chunk_index=chunk.chunk_index,
                        language=chunk.language,
                        category=chunk.category,
                        section=chunk.section,
                        page_number=chunk.page_number,
                        embedding_model=settings.embedding_model,
                    ),
                )
                for chunk, vector in zip(stored, vectors, strict=True)
            ]
        )
    except VectorStoreUnavailableError as exc:
        logger.warning("vector_store_mirror_skipped", error=str(exc))
    except Exception as exc:
        logger.warning("vector_store_mirror_failed", error_type=type(exc).__name__)
