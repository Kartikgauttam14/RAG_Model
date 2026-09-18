import asyncio
import shlex
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_roles
from app.config import Settings, get_settings
from app.database import get_db
from app.database.models import (
    AuditLog,
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobStatus,
    Role,
)
from app.ingestion import (
    SITEMAP_PATHS,
    SPA_EXTRACTION_HINT,
    is_ingestible_content_type,
    is_unrendered_spa,
    media_type_for,
    normalize_site_paths,
    parse_sitemap_urls,
    source_document_name,
)
from app.security import (
    UnsafeURLError,
    content_sha256,
    safe_filename,
    validate_ingestion_url,
    validate_upload,
)
from app.workers.ingestion import index_document

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentResponse(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
    media_type: str
    access_scope: str
    status: str
    current_version: int
    created_at: datetime


class IngestionJobResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    progress: float
    stage: str
    error_code: str | None
    error_message: str | None


class UrlIngestionRequest(BaseModel):
    url: str
    access_scope: str = "tenant"
    category: str | None = None
    tags: str | None = None
    is_authoritative: bool = False


class SiteIngestionRequest(BaseModel):
    """Ingest a whole site: the landing page plus optional page/API paths on the same host."""

    url: str
    paths: list[str] = []
    access_scope: str = "tenant"
    category: str | None = None
    tags: str | None = None
    is_authoritative: bool = False


class SiteSourceResult(BaseModel):
    url: str
    status: str
    media_type: str | None = None
    document_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    detail: str | None = None


class SiteIngestionResponse(BaseModel):
    requested: int
    queued: int
    results: list[SiteSourceResult]


@router.post("", response_model=IngestionJobResponse, status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    access_scope: str = Form("tenant"),
    category: str | None = Form(None),
    tags: str | None = Form(None),
    is_authoritative: bool = Form(False),
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IngestionJobResponse:
    data = await file.read()
    filename = safe_filename(file.filename or "upload")
    media_type = file.content_type or "application/octet-stream"
    try:
        validate_upload(
            data=data,
            filename=filename,
            media_type=media_type,
            allowed_types=settings.allowed_upload_types,
            max_size_mb=settings.max_upload_size_mb,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if access_scope not in {
        "public",
        "tenant",
        f"role:{Role.user.value}",
        f"role:{Role.editor.value}",
    }:
        raise HTTPException(status_code=422, detail="Invalid access scope")
    digest = content_sha256(data)
    existing = await db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == principal.tenant_id,
            DocumentVersion.content_hash == digest,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="This content has already been ingested")
    storage_dir = Path("uploads") / principal.tenant_id
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{uuid.uuid4()}-{filename}"
    storage_path.write_bytes(data)
    await _scan_file(storage_path, settings.malware_scanner_command)
    document = Document(
        tenant_id=principal.tenant_id,
        name=filename,
        source_type="upload",
        media_type=media_type,
        access_scope=access_scope,
        status=DocumentStatus.pending,
        current_version=1,
        created_by=principal.user_id,
    )
    db.add(document)
    await db.flush()
    version = DocumentVersion(
        document_id=document.id,
        tenant_id=principal.tenant_id,
        version=1,
        content_hash=digest,
        storage_path=str(storage_path.resolve()),
        extracted_metadata={
            "category": category,
            "tags": [item.strip() for item in (tags or "").split(",") if item.strip()],
        },
        is_authoritative=is_authoritative and principal.role == Role.admin,
    )
    db.add(version)
    await db.flush()
    job = IngestionJob(
        document_id=document.id,
        document_version_id=version.id,
        status=JobStatus.queued,
    )
    db.add(job)
    db.add(
        AuditLog(
            actor_user_id=principal.user_id,
            action="document.upload",
            resource_type="document",
            resource_id=str(document.id),
            metadata_json={
                "content_hash": digest,
                "access_scope": access_scope,
                "is_authoritative": is_authoritative and principal.role == Role.admin,
            },
            created_at=datetime.now(UTC),
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        storage_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="Duplicate document") from exc
    index_document.delay(str(job.id))
    return _job_response(job)


@router.post("/url", response_model=IngestionJobResponse, status_code=202)
async def ingest_url(
    payload: UrlIngestionRequest,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IngestionJobResponse:
    if not settings.url_ingestion_enabled:
        raise HTTPException(status_code=403, detail="URL ingestion is disabled")
    try:
        url = validate_ingestion_url(payload.url, settings.url_allowed_hosts)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data, content_type = await _fetch_remote(url, settings)
    _, job = await _store_remote_document(
        url=url,
        data=data,
        content_type=content_type,
        principal=principal,
        db=db,
        access_scope=payload.access_scope,
        category=payload.category,
        tags=payload.tags,
        is_authoritative=payload.is_authoritative,
    )
    return _job_response(job)


@router.post("/site", response_model=SiteIngestionResponse, status_code=202)
async def ingest_site(
    payload: SiteIngestionRequest,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SiteIngestionResponse:
    """Ingest many sources from one host in a single call.

    Pass explicit ``paths`` (pages or JSON API endpoints) or leave them empty to try the site's
    ``sitemap.xml`` first. Each source is stored through the same ingestion pipeline as uploads;
    individual failures are reported per source instead of failing the whole request.
    """
    if not settings.url_ingestion_enabled:
        raise HTTPException(status_code=403, detail="URL ingestion is disabled")
    _validate_access_scope(payload.access_scope)
    try:
        base_url = validate_ingestion_url(payload.url, settings.url_allowed_hosts)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    discovered = payload.paths or await _discover_sitemap_paths(base_url, settings)
    targets = normalize_site_paths(base_url, discovered)[: settings.url_ingestion_max_pages]
    results: list[SiteSourceResult] = []
    for target in targets:
        try:
            source_url = validate_ingestion_url(target, settings.url_allowed_hosts)
        except UnsafeURLError as exc:
            results.append(SiteSourceResult(url=target, status="skipped", detail=str(exc)))
            continue
        try:
            data, content_type = await _fetch_remote(source_url, settings)
            document, job = await _store_remote_document(
                url=source_url,
                data=data,
                content_type=content_type,
                principal=principal,
                db=db,
                access_scope=payload.access_scope,
                category=payload.category,
                tags=payload.tags,
                is_authoritative=payload.is_authoritative,
            )
        except HTTPException as exc:
            results.append(
                SiteSourceResult(
                    url=source_url,
                    status="duplicate" if exc.status_code == 409 else "failed",
                    detail=str(exc.detail),
                )
            )
            continue
        results.append(
            SiteSourceResult(
                url=source_url,
                status="queued",
                media_type=document.media_type,
                document_id=document.id,
                job_id=job.id,
            )
        )
    queued = sum(1 for item in results if item.status == "queued")
    return SiteIngestionResponse(requested=len(targets), queued=queued, results=results)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentResponse]:
    rows = await db.scalars(
        select(Document)
        .where(
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    )
    return [_document_response(item) for item in rows]


@router.post("/{document_id:uuid}/reindex", response_model=IngestionJobResponse, status_code=202)
async def reindex_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> IngestionJobResponse:
    document = await db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    version = await db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version == document.current_version,
        )
    )
    if not version:
        raise HTTPException(status_code=409, detail="Current document version is missing")
    job = IngestionJob(document_id=document.id, document_version_id=version.id)
    document.status = DocumentStatus.pending
    db.add(job)
    await db.commit()
    index_document.delay(str(job.id))
    return _job_response(job)


@router.get("/jobs/{job_id}", response_model=IngestionJobResponse)
async def get_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> IngestionJobResponse:
    job = await db.scalar(
        select(IngestionJob)
        .join(Document, Document.id == IngestionJob.document_id)
        .where(IngestionJob.id == job_id, Document.tenant_id == principal.tenant_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    return _job_response(job)


@router.get("/{document_id:uuid}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    document = await db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return _document_response(document)


@router.delete("/{document_id:uuid}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> None:
    document = await db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    document.deleted_at = datetime.now(UTC)
    document.status = DocumentStatus.deleted
    db.add(
        AuditLog(
            actor_user_id=principal.user_id,
            action="document.delete",
            resource_type="document",
            resource_id=str(document.id),
            metadata_json={},
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()


@router.get("/{document_id:uuid}/chunks")
async def inspect_chunks(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.editor, Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await db.execute(
        select(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.document_id == document_id, Document.tenant_id == principal.tenant_id)
        .order_by(DocumentChunk.chunk_index)
        .limit(500)
    )
    return [
        {
            "id": str(chunk.id),
            "index": chunk.chunk_index,
            "section": chunk.section,
            "page": chunk.page_number,
            "language": chunk.language,
            "content": chunk.content,
            "metadata": chunk.metadata_json,
        }
        for chunk in rows.scalars()
    ]


async def _scan_file(path: Path, command: str | None) -> None:
    if not command:
        return
    arguments = [*shlex.split(command), str(path)]
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Malware scan failed: {stderr.decode()[:200]}")


async def _fetch_remote(url: str, settings: Settings) -> tuple[bytes, str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        try:
            response = await client.get(url, headers={"User-Agent": settings.url_ingestion_user_agent})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=422, detail=f"Could not fetch URL: {exc}") from exc
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not is_ingestible_content_type(content_type):
        raise HTTPException(status_code=422, detail=f"Unsupported content type: {content_type or 'unknown'}")
    data = response.content
    if len(data) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Page exceeds the maximum ingestion size")
    if content_type in {"text/html", "application/xhtml+xml"} and is_unrendered_spa(
        data.decode("utf-8", errors="replace")
    ):
        raise HTTPException(status_code=422, detail=SPA_EXTRACTION_HINT)
    return data, content_type


async def _discover_sitemap_paths(base_url: str, settings: Settings) -> list[str]:
    for path in SITEMAP_PATHS:
        try:
            sitemap_url = validate_ingestion_url(urljoin(base_url, path), settings.url_allowed_hosts)
        except UnsafeURLError:
            continue
        try:
            data, _ = await _fetch_remote(sitemap_url, settings)
        except HTTPException:
            continue
        urls = parse_sitemap_urls(
            data.decode("utf-8", errors="replace"), base_url, settings.url_ingestion_max_pages
        )
        if urls:
            return urls
    return []


def _validate_access_scope(access_scope: str) -> None:
    allowed = {"public", "tenant", f"role:{Role.user.value}", f"role:{Role.editor.value}"}
    if access_scope not in allowed:
        raise HTTPException(status_code=422, detail="Invalid access scope")


async def _store_remote_document(
    *,
    url: str,
    data: bytes,
    content_type: str,
    principal: Principal,
    db: AsyncSession,
    access_scope: str,
    category: str | None,
    tags: str | None,
    is_authoritative: bool,
) -> tuple[Document, IngestionJob]:
    _validate_access_scope(access_scope)
    digest = content_sha256(data)
    existing = await db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == principal.tenant_id,
            DocumentVersion.content_hash == digest,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="This content has already been ingested")
    media_type = media_type_for(content_type)
    filename = safe_filename(source_document_name(url, content_type))
    storage_dir = Path("uploads") / principal.tenant_id
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{uuid.uuid4()}-{filename}"
    storage_path.write_bytes(data)
    document = Document(
        tenant_id=principal.tenant_id,
        name=filename,
        source_type="url",
        media_type=media_type,
        access_scope=access_scope,
        status=DocumentStatus.pending,
        current_version=1,
        created_by=principal.user_id,
    )
    db.add(document)
    await db.flush()
    version = DocumentVersion(
        document_id=document.id,
        tenant_id=principal.tenant_id,
        version=1,
        content_hash=digest,
        storage_path=str(storage_path.resolve()),
        extracted_metadata={
            "source_url": url,
            "source_content_type": content_type,
            "category": category,
            "tags": [item.strip() for item in (tags or "").split(",") if item.strip()],
        },
        is_authoritative=is_authoritative and principal.role == Role.admin,
    )
    db.add(version)
    await db.flush()
    job = IngestionJob(
        document_id=document.id,
        document_version_id=version.id,
        status=JobStatus.queued,
    )
    db.add(job)
    db.add(
        AuditLog(
            actor_user_id=principal.user_id,
            action="document.ingest_url",
            resource_type="document",
            resource_id=str(document.id),
            metadata_json={
                "source_url": url,
                "content_hash": digest,
                "access_scope": access_scope,
                "media_type": media_type,
            },
            created_at=datetime.now(UTC),
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        storage_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="Duplicate document") from exc
    index_document.delay(str(job.id))
    return document, job


def _document_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        name=document.name,
        source_type=document.source_type,
        media_type=document.media_type,
        access_scope=document.access_scope,
        status=document.status.value,
        current_version=document.current_version,
        created_at=document.created_at,
    )


def _job_response(job: IngestionJob) -> IngestionJobResponse:
    return IngestionJobResponse(
        id=job.id,
        document_id=job.document_id,
        status=job.status.value,
        progress=job.progress,
        stage=job.stage,
        error_code=job.error_code,
        error_message=job.error_message,
    )
